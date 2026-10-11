"""Прогноз погоды (Open-Meteo): сводка по Украине, поле давления и циклоны, ветер на высоте."""

import asyncio
import logging
import ssl
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import timedelta
from html import escape

import aiohttp
import numpy as np

from .cities import MACRO_REGIONS, City, CityIndex, region_name
from .config import Config
from .geo import bearing, compass8, haversine, upsample
from .texts import PLACES, fmt_date, fmt_temp, fmt_temp_range, local_now, tr

log = logging.getLogger("forecast")

API = "https://api.open-meteo.com/v1/forecast"
POINTS_PER_MINUTE = 420
UKRAINE = (49.0, 32.0)

# Сетка для поля давления: Европа с шагом 3° по широте и 4° по долготе (давление меняется плавно,
# а бесплатный Open-Meteo считает каждую точку отдельным запросом — лимит 600 в минуту).
GRID_LATS = np.arange(24.0, 69.1, 3.0)
GRID_LONS = np.arange(-20.0, 68.1, 4.0)
GRID_CHUNK = 92  # точек в одном запросе

# Сетка для карты осадков: кадр обзорной карты Украины, шаг 0,75° × 1,1° (около 80 км).
PRECIP_LATS = np.arange(43.4, 54.0, 0.75)
PRECIP_LONS = np.arange(20.2, 42.3, 1.1)
PRECIP_CHUNK = 80
PRECIP_LEVELS = (0.3, 1.0, 3.0, 6.0, 10.0, 20.0)  # границы градаций на карте, мм за 3 часа
PRECIP_LEVELS_HOUR = (0.1, 0.5, 1.5, 3.0, 6.0, 12.0)  # то же для карты «сейчас», мм за час


class OpenMeteo:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession, ssl_ctx: ssl.SSLContext):
        self.cfg, self.session, self.ssl_ctx = cfg, session, ssl_ctx
        self._spent: deque[tuple[float, int]] = deque()  # (когда, сколько точек) за последнюю минуту
        self._gate = asyncio.Lock()

    async def _throttle(self, points: int) -> None:
        """Бесплатный Open-Meteo считает каждую точку запросом и даёт 600 в минуту — держимся ниже."""
        async with self._gate:
            while True:
                now = time.monotonic()
                while self._spent and now - self._spent[0][0] > 60:
                    self._spent.popleft()
                if not self._spent or sum(n for _, n in self._spent) + points <= POINTS_PER_MINUTE:
                    break
                await asyncio.sleep(60 - (now - self._spent[0][0]) + 0.5)
            self._spent.append((time.monotonic(), points))

    async def _get(self, lats, lons, **params) -> list[dict]:
        query = {"latitude": ",".join(f"{v:.3f}" for v in lats), "longitude": ",".join(f"{v:.3f}" for v in lons), **params}
        for attempt in range(3):
            await self._throttle(len(lats))
            try:
                async with self.session.get(API, params=query, ssl=self.ssl_ctx,
                                            timeout=aiohttp.ClientTimeout(total=40)) as resp:
                    data = await resp.json()
                    if resp.status == 200:
                        return data if isinstance(data, list) else [data]
                    raise RuntimeError(f"Open-Meteo {resp.status}: {data.get('reason', data)}")
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                if attempt == 2:
                    raise
                log.warning("Open-Meteo: %s — повтор", exc or type(exc).__name__)
                # 429 — исчерпан минутный лимит (его делят все программы с этого адреса): ждём новую минуту.
                await asyncio.sleep(61 if "429" in str(exc) else 5 * (attempt + 1))
        return []

    async def cities(self, cities: list[City]) -> list[dict]:
        return await self._get(
            [c.lat for c in cities], [c.lon for c in cities],
            daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_gusts_10m_max",
            hourly="weather_code", wind_speed_unit="ms", timezone=self.cfg.timezone, forecast_days=3,
        )

    async def pressure(self) -> "PressureField":
        """Давление на уровне моря над Европой: сейчас и через 24 часа."""
        lat_grid, lon_grid = np.meshgrid(GRID_LATS, GRID_LONS, indexing="ij")
        lats, lons = lat_grid.ravel(), lon_grid.ravel()
        now = time.time()
        frames = np.empty((2, len(lats)))
        for start in range(0, len(lats), GRID_CHUNK):
            part = slice(start, start + GRID_CHUNK)
            rows = await self._get(lats[part], lons[part], hourly="pressure_msl", forecast_days=3,
                                   timeformat="unixtime", timezone="GMT")
            for offset, row in enumerate(rows):
                times = np.array(row["hourly"]["time"], dtype=float)
                values = np.array(row["hourly"]["pressure_msl"], dtype=float)
                for f, t in enumerate((now, now + 86400)):
                    frames[f, start + offset] = values[int(np.argmin(np.abs(times - t)))]
            await asyncio.sleep(2)
        shape = lat_grid.shape
        return PressureField(GRID_LATS, GRID_LONS, frames[0].reshape(shape), frames[1].reshape(shape))

    async def point(self, lat: float, lon: float) -> dict:
        """Подробный прогноз для одной точки: сейчас, по часам и на три дня (для ответов в чате)."""
        return (await self._get(
            [lat], [lon],
            current="temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_gusts_10m,"
                    "relative_humidity_2m,pressure_msl,precipitation",
            hourly="temperature_2m,weather_code,precipitation,precipitation_probability",
            daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_gusts_10m_max",
            wind_speed_unit="ms", timezone=self.cfg.timezone, forecast_days=3,
        ))[0]

    async def precipitation(self, hours: int = 3) -> "PrecipField":
        """Сумма осадков за ближайшие hours часов по сетке над Украиной и соседями."""
        lat_grid, lon_grid = np.meshgrid(PRECIP_LATS, PRECIP_LONS, indexing="ij")
        lats, lons = lat_grid.ravel(), lon_grid.ravel()
        now = time.time()
        hourly = np.zeros((hours, len(lats)))
        storms = np.zeros((hours, len(lats)), dtype=bool)
        start = now
        for first in range(0, len(lats), PRECIP_CHUNK):
            part = slice(first, first + PRECIP_CHUNK)
            rows = await self._get(lats[part], lons[part], hourly="precipitation,weather_code", forecast_days=2,
                                   timeformat="unixtime", timezone="GMT")
            for offset, row in enumerate(rows):
                times = np.array(row["hourly"]["time"], dtype=float)
                # Значение на отметке T — это осадки за час ДО неё, поэтому берём три ближайшие будущие отметки.
                i = int(np.searchsorted(times, now, side="right"))
                hourly[:, first + offset] = [v or 0.0 for v in row["hourly"]["precipitation"][i:i + hours]]
                storms[:, first + offset] = [(c or 0) >= 95 for c in row["hourly"]["weather_code"][i:i + hours]]
                start = times[i] - 3600
            await asyncio.sleep(2)
        shape = (hours, *lat_grid.shape)
        return PrecipField.from_hours(PRECIP_LATS, PRECIP_LONS, hourly.reshape(shape), storms.reshape(shape), float(start))

    async def steering(self, points: list[tuple[float, float]]) -> list[tuple[float, float] | None]:
        """Ветер на высоте ~3 км в каждой точке: грозы обычно смещаются вместе с ним.

        Возвращает для каждой точки (км/ч, азимут «куда») или None, если данных нет.
        """
        try:
            rows = await self._get([p[0] for p in points], [p[1] for p in points],
                                   hourly="wind_speed_700hPa,wind_direction_700hPa",
                                   forecast_days=2, timeformat="unixtime", timezone="GMT")
        except Exception as exc:
            log.warning("ветер на высоте недоступен: %s", exc or type(exc).__name__)
            return [None] * len(points)
        result = []
        for row in rows:
            hourly = row["hourly"]
            i = int(np.argmin(np.abs(np.array(hourly["time"], dtype=float) - time.time())))
            speed, direction = hourly["wind_speed_700hPa"][i], hourly["wind_direction_700hPa"][i]
            # Очаги идут чуть медленнее потока; направление ветра — «откуда», а нам нужно «куда».
            ok = speed is not None and direction is not None
            result.append((float(speed) * 0.85, (float(direction) + 180) % 360) if ok else None)
        return result


# ---------- сводка прогноза ----------

# Код погоды WMO → (ключ фразы, значок для текста, значок для карты). Порядок ключей — от спокойной к опасной.
WX_ORDER = ["clear", "partly", "cloudy", "fog", "drizzle", "rain", "showers", "snow", "heavy_rain", "freezing",
            "heavy_snow", "thunder", "hail"]
WX_EMOJI = {"clear": "☀️", "partly": "⛅", "cloudy": "☁️", "fog": "🌫", "drizzle": "🌦", "rain": "🌧", "showers": "🌧",
            "snow": "🌨", "heavy_rain": "🌧", "freezing": "🧊", "heavy_snow": "❄️", "thunder": "⛈", "hail": "⛈"}
WX_GLYPH = {"clear": "☀", "partly": "☀", "cloudy": "☁", "fog": "☁", "drizzle": "☂", "rain": "☂", "showers": "☂",
            "snow": "❄", "heavy_rain": "☂", "freezing": "☂", "heavy_snow": "❄", "thunder": "⚡", "hail": "⚡"}
WET = set(WX_ORDER[4:])


def wx_key(code: int) -> str:
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if code in (51, 53, 55):
        return "drizzle"
    if code in (56, 57, 66, 67):
        return "freezing"
    if code in (61, 63):
        return "rain"
    if code == 65:
        return "heavy_rain"
    if code in (71, 73, 77, 85):
        return "snow"
    if code in (75, 86):
        return "heavy_snow"
    if code in (80, 81, 82):
        return "showers"
    if code == 95:
        return "thunder"
    if code in (96, 99):
        return "hail"
    return "cloudy"


@dataclass
class Digest:
    text: str
    title: str
    points: list[tuple[float, float, float, str]]  # для карты: широта, долгота, температура днём, значок


async def build_digest(om: OpenMeteo, index: CityIndex, cfg: Config, day: int, warnings_line: str = "") -> Digest:
    """Сводка на сегодня (day=0) или завтра (day=1) по областным центрам."""
    lang, t = cfg.language, tr(cfg.language)
    groups = {key: [c for c in map(index.by_name, names) if c] for key, names in MACRO_REGIONS.items()}
    cities = [c for group in groups.values() for c in group]
    data = dict(zip((c.key for c in cities), await om.cities(cities)))
    date = local_now(cfg.timezone) + timedelta(days=day)
    kind = "today" if day == 0 else "tomorrow"
    lines = [t[f"digest_{kind}"].format(date=fmt_date(lang, date)), ""]

    thunder, gusts, rain, frost, points = [], [], [], [], []
    for key, group in groups.items():
        tmax, tmin, keys, gust = [], [], [], 0.0
        for city in group:
            daily, hourly = data[city.key]["daily"], data[city.key]["hourly"]
            night = daily["temperature_2m_min"][1]  # ночь перед завтрашним днём — и для «сегодня», и для «завтра»
            if daily["temperature_2m_max"][day] is None or night is None:
                continue
            wx = wx_key(int(daily["weather_code"][day] or 0))
            tmax.append(daily["temperature_2m_max"][day])
            tmin.append(night)
            keys.append(wx)
            city_gust = daily["wind_gusts_10m_max"][day] or 0.0
            gust = max(gust, city_gust)
            name = escape(city.name(lang))
            points.append((city.lat, city.lon, daily["temperature_2m_max"][day], WX_GLYPH[wx]))
            hours = [h for h in range(24) if (hourly["weather_code"][day * 24 + h] or 0) >= 95]
            if hours:
                thunder.append(f"{name} ({t['day_parts'][hours[0] // 6]})")
            if city_gust >= 15:
                gusts.append(f"{name} ({city_gust:.0f} м/с)")
            if (daily["precipitation_sum"][day] or 0) >= 10:
                rain.append(f"{name} ({daily['precipitation_sum'][day]:.0f} мм)")
            if night <= -10:
                frost.append(f"{name} ({fmt_temp(night)})")
        if not keys:
            continue
        common = Counter(keys).most_common(1)[0][0]
        worst = max(keys, key=WX_ORDER.index)
        sky = t["wx"][common]
        if worst != common and worst in WET:
            sky += f", {t['locally']}{t['wx'][worst]}"
        wind = t["digest_wind"].format(gust=round(gust)) if gust >= 10 else ""
        lines.append(t["digest_line"].format(emoji=WX_EMOJI[worst if worst in WET else common], region=t["macro"][key],
                                             tmax=fmt_temp_range(tmax), tmin=fmt_temp_range(tmin), sky=sky, wind=wind))
    extra = [t[k].format(cities=", ".join(v)) for k, v in (("digest_thunder", thunder), ("digest_gusts", gusts),
                                                          ("digest_rain", rain), ("digest_frost", frost)) if v]
    if extra:
        lines += ["", *extra]
    if warnings_line:
        lines += ["", warnings_line]
    lines += ["", t["digest_footer"]]
    title = t[f"digest_title_map_{kind}"].format(date=fmt_date(lang, date).upper())
    return Digest("\n".join(lines), title, points)


# ---------- давление и циклоны ----------

@dataclass
class Centre:
    lat: float
    lon: float
    pressure: float
    next: "Centre | None" = None  # где этот циклон окажется через сутки

    @property
    def dist_ua(self) -> float:
        return float(haversine(self.lat, self.lon, *UKRAINE))


@dataclass
class PressureField:
    lats: np.ndarray
    lons: np.ndarray
    now: np.ndarray       # (широты, долготы), гПа
    tomorrow: np.ndarray

    def at(self, grid: np.ndarray, lat: float, lon: float) -> float:
        i = int(np.argmin(np.abs(self.lats - lat)))
        j = int(np.argmin(np.abs(self.lons - lon)))
        return float(grid[i, j])


def find_centres(field: PressureField, grid: np.ndarray, low: bool) -> list[Centre]:
    """Центры циклонов (low=True) или антициклонов: локальные экстремумы давления в окне ±5°."""
    k = 4
    fine = upsample(grid, k)
    lats = np.linspace(field.lats[0], field.lats[-1], fine.shape[0])
    lons = np.linspace(field.lons[0], field.lons[-1], fine.shape[1])
    signed = fine if low else -fine
    # Полуразмер окна 5° в ячейках уплотнённой сетки — отдельно по широте и долготе.
    ri = round(5 * k / (field.lats[1] - field.lats[0]))
    rj = round(5 * k / (field.lons[1] - field.lons[0]))
    padded = np.pad(signed, ((ri, ri), (rj, rj)), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, (2 * ri + 1, 2 * rj + 1))
    is_extreme = signed <= windows.min(axis=(2, 3))
    depth = windows.mean(axis=(2, 3)) - signed  # насколько центр «глубже» окрестностей
    centres = []
    for i, j in zip(*np.nonzero(is_extreme)):
        on_edge = i < k or j < k or i >= fine.shape[0] - k or j >= fine.shape[1] - k
        p = float(fine[i, j])
        if on_edge or depth[i, j] < 2.5 or (p > 1008 if low else p < 1020):
            continue
        if all(haversine(lats[i], lons[j], c.lat, c.lon) > 350 for c in centres):
            centres.append(Centre(float(lats[i]), float(lons[j]), p))
    return centres


def place_name(lat: float, lon: float, lang: str) -> str:
    row = min(PLACES, key=lambda p: haversine(lat, lon, p[0], p[1]))
    return row[3] if lang == "uk" else row[2]


@dataclass
class Synoptic:
    text: str
    title: str
    lows: list[Centre]
    highs: list[Centre]
    deep: bool  # к Украине идёт глубокий циклон — повод для внеочередного поста


def build_synoptic(field: PressureField, cfg: Config) -> Synoptic:
    lang, t = cfg.language, tr(cfg.language)
    lows = find_centres(field, field.now, low=True)
    highs_now = find_centres(field, field.now, low=False)
    lows_next = find_centres(field, field.tomorrow, low=True)
    for low in lows:
        near = [c for c in lows_next if haversine(low.lat, low.lon, c.lat, c.lon) < 1300]
        low.next = min(near, key=lambda c: haversine(low.lat, low.lon, c.lat, c.lon), default=None)

    ua_now = field.at(field.now, *UKRAINE)
    # Глубокий циклон влияет дальше мелкого: каждый гПа глубины «приближает» его на 60 км.
    main_low = min((c for c in lows if c.dist_ua < 1800), key=lambda c: c.dist_ua - (1013 - c.pressure) * 60, default=None)
    main_high = min((c for c in highs_now if c.dist_ua < 1800), key=lambda c: c.dist_ua, default=None)
    if main_low and main_high:
        # Спорный случай решает само давление над Украиной: низкое — хозяин циклон, высокое — антициклон.
        if ua_now >= 1018 or (ua_now > 1012 and main_high.dist_ua < main_low.dist_ua):
            main_low = None
        else:
            main_high = None

    date = fmt_date(lang, local_now(cfg.timezone))
    lines = [t["syn_title"].format(date=date), ""]
    deep = any(c.pressure <= 995 and c.dist_ua < 900 for c in lows_next) or any(
        c.pressure <= 995 and c.dist_ua < 700 for c in lows)
    if deep:
        lines += [t["syn_deep"], ""]

    if main_low:
        lines.append(t["syn_main_low"].format(where=place_name(main_low.lat, main_low.lon, lang), p=round(main_low.pressure)))
        nxt = main_low.next
        if nxt is None:
            lines.append(t["syn_low_gone"])
        else:
            delta = nxt.pressure - main_low.pressure
            change = t["syn_deepens" if delta < 0 else "syn_fills"].format(p=round(nxt.pressure)) if abs(delta) >= 3 else ""
            if haversine(main_low.lat, main_low.lon, nxt.lat, nxt.lon) < 250:
                lines.append(t["syn_low_stays"].format(change=change))
            else:
                direction = t["dirs_to"][compass8(bearing(main_low.lat, main_low.lon, nxt.lat, nxt.lon))]
                lines.append(t["syn_low_moves"].format(dir=direction, where=place_name(nxt.lat, nxt.lon, lang), change=change))
    elif main_high:
        lines.append(t["syn_main_high"].format(where=place_name(main_high.lat, main_high.lon, lang), p=round(main_high.pressure)))
    else:
        lines.append(t["syn_main_none"])

    others = [t["syn_other_low"].format(where=place_name(c.lat, c.lon, lang), p=round(c.pressure))
              for c in sorted(lows, key=lambda c: c.dist_ua) if c is not main_low and c.dist_ua < 3000][:2]
    others += [t["syn_other_high"].format(where=place_name(c.lat, c.lon, lang), p=round(c.pressure))
               for c in sorted(highs_now, key=lambda c: c.dist_ua) if c is not main_high and c.dist_ua < 3000][:2]
    if others:
        lines += ["", t["syn_others"], *others]

    kyiv_now, kyiv_next = field.at(field.now, 50.45, 30.52), field.at(field.tomorrow, 50.45, 30.52)
    d = round(kyiv_next - kyiv_now)
    change = t["syn_kyiv_same"] if abs(d) < 2 else t["syn_kyiv_up" if d > 0 else "syn_kyiv_down"].format(d=abs(d))
    lines += ["", t["syn_kyiv"].format(p=round(kyiv_now), change=change)]
    if main_low:
        lines.append(t["syn_meaning_low" if main_low.dist_ua < 900 else "syn_meaning_low_far"])
    elif main_high:
        lines.append(t["syn_meaning_high"])
    lines += ["", t["syn_footer"]]
    title = t["syn_title_map"].format(date=date.upper())
    return Synoptic("\n".join(lines), title, lows, highs_now, deep)


# ---------- осадки на ближайшие часы ----------

@dataclass
class PrecipField:
    lats: np.ndarray
    lons: np.ndarray
    total: np.ndarray     # (широты, долготы), мм за период
    thunder: np.ndarray   # там же: ожидается ли гроза
    start: float          # начало и конец периода, unix
    end: float
    hourly: np.ndarray | None = None          # (часы, широты, долготы) — из чего сложена сумма
    hourly_thunder: np.ndarray | None = None

    @staticmethod
    def from_hours(lats, lons, hourly: np.ndarray, thunder: np.ndarray, start: float) -> "PrecipField":
        return PrecipField(lats, lons, hourly.sum(axis=0), thunder.any(axis=0), start, start + 3600 * len(hourly),
                           hourly, thunder)

    def head(self, hours: int) -> "PrecipField":
        """То же поле, но только за первые hours часов (первый час — это «сейчас»)."""
        return PrecipField.from_hours(self.lats, self.lons, self.hourly[:hours], self.hourly_thunder[:hours], self.start)

    def tail(self, hours: int) -> "PrecipField":
        """То же поле без первых hours часов — когда они уже прошли."""
        return PrecipField.from_hours(self.lats, self.lons, self.hourly[hours:], self.hourly_thunder[hours:],
                                      self.start + 3600 * hours)

    def fine(self, k: int = 6) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Сглаженное поле на уплотнённой сетке: (широты, долготы, мм)."""
        grid = np.clip(upsample(self.total, k), 0, None)
        return (np.linspace(self.lats[0], self.lats[-1], grid.shape[0]),
                np.linspace(self.lons[0], self.lons[-1], grid.shape[1]), grid)


@dataclass
class Nowcast:
    text: str
    title: str


def _mm(value: float, lang: str) -> str:
    if value < 1:
        return "менше 1 мм" if lang == "uk" else "менее 1 мм"
    return f"{value:.0f} мм"


def build_nowcast(field: PrecipField, index: CityIndex, cfg: Config, current: bool = False) -> Nowcast:
    """Текст к карте осадков: в каких областях и городах ждать дождя в ближайшие часы.

    current=True — карта «сейчас»: осадки за текущий час, пороги градаций соответственно ниже.
    """
    lang, t = cfg.language, tr(cfg.language)
    heavy, moderate, light = (4.0, 1.0, 0.2) if current else (10.0, 3.0, 0.5)
    kind = "now" if current else "nowcast"
    lats, lons, grid = field.fine()
    rows = np.clip(np.searchsorted(lats, index.lat), 0, len(lats) - 1)
    cols = np.clip(np.searchsorted(lons, index.lon), 0, len(lons) - 1)
    amount = grid[rows, cols]  # мм в каждом населённом пункте
    coarse_i = np.abs(field.lats[:, None] - index.lat[None, :]).argmin(axis=0)
    coarse_j = np.abs(field.lons[:, None] - index.lon[None, :]).argmin(axis=0)
    stormy = field.thunder[coarse_i, coarse_j] & (amount >= PRECIP_LEVELS[0])

    by_region: dict[str, float] = {}
    thunder_regions: dict[str, None] = {}
    for city, mm, storm in zip(index.cities, amount, stormy):
        by_region[city.region] = max(by_region.get(city.region, 0.0), float(mm))
        if storm:
            thunder_regions[city.region] = None

    clock = lambda ts: local_now(cfg.timezone, ts).strftime("%H:%M")
    hours = round((field.end - field.start) / 3600)
    span = dict(hours=hours, start=clock(field.start), end=clock(field.end))
    lines = [t[f"{kind}_title"].format(**span), ""]
    wet = {code: mm for code, mm in by_region.items() if mm >= light}
    if not wet:
        lines.append(t[f"{kind}_none"].format(**span))
    else:
        bands = (("nowcast_heavy", heavy, float("inf")), ("nowcast_moderate", moderate, heavy), ("nowcast_light", light, moderate))
        for key, low, high in bands:
            codes = sorted((c for c, mm in wet.items() if low <= mm < high), key=lambda c: -wet[c])
            if codes:
                lines.append(t[key].format(lo=low, hi=high, regions=", ".join(region_name(c, lang) for c in codes)))
        big = sorted(((c, float(mm)) for c, mm in zip(index.cities, amount) if c.pop >= 200_000 and mm >= light),
                     key=lambda pair: -pair[1])[:7]
        if big:
            lines += ["", t["nowcast_cities"].format(cities=", ".join(f"{escape(c.name(lang))} — {_mm(mm, lang)}" for c, mm in big))]
        if thunder_regions:
            lines.append(t["nowcast_thunder"].format(regions=", ".join(region_name(c, lang) for c in thunder_regions)))
        if len(wet) < len(by_region):
            lines += ["", t["nowcast_dry"]]
    lines += ["", t[f"{kind}_footer"].format(time=clock(time.time()))]
    title = t[f"{kind}_title_map"].format(**span)
    return Nowcast("\n".join(lines), title)
