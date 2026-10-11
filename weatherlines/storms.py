"""Грозовые очаги: поиск по разрядам, сопровождение, скорость и направление, зона прогноза («конус»)."""

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from .cities import City, CityIndex
from .config import Config
from .geo import destination, from_xy, haversine, to_xy

MAX_POINTS = 6000          # больше разрядов за окно в кластеризацию не берём
MIN_RADIUS_KM = 8.0
MAX_RADIUS_KM = 70.0
STATIONARY_KMH = 7.0       # медленнее — считаем очаг малоподвижным
MAX_SPEED_KMH = 140.0      # быстрее грозы не ходят — значит, оценка движения ошибочна
SPREAD_TRACK_DEG = 18.0    # полураствор конуса, если движение измерено по разрядам
SPREAD_WIND_DEG = 28.0     # …и если взято из ветра на высоте (менее надёжно)
LOST_AFTER_SEC = 15 * 60   # очаг без разрядов столько времени считается распавшимся

# Пороги интенсивности, разрядов в минуту: слабая / умеренная / сильная / очень сильная
LEVELS = (2.0, 8.0, 25.0)


@dataclass
class CityHit:
    city: City
    dist_km: float          # от центра очага
    eta_min: float | None   # через сколько минут подойдёт гроза (None — уже в зоне или очаг стоит)


@dataclass
class Cell:
    id: int
    lat: float
    lon: float
    radius_km: float
    n: int                          # разрядов за окно window_min
    rate: float                     # разрядов в минуту
    trend: int                      # +1 усиливается, -1 слабеет, 0 без изменений
    strikes: np.ndarray             # (n, 3): время, широта, долгота
    first_seen: float
    last_seen: float
    speed_kmh: float | None = None  # None — движение пока неизвестно; 0 — малоподвижна
    bearing: float | None = None    # куда движется, градусы от севера
    motion_src: str = "none"        # track — по разрядам, wind — по ветру на высоте
    in_circle: list[CityHit] = field(default_factory=list)
    in_cone: list[CityHit] = field(default_factory=list)
    nearest: City | None = None
    nearest_km: float = 0.0
    wind: tuple[float, float] | None = None  # запасная оценка движения (км/ч, азимут)

    @property
    def level(self) -> int:
        return sum(self.rate >= x for x in LEVELS)

    @property
    def moving(self) -> bool:
        return bool(self.speed_kmh) and self.bearing is not None

    @property
    def spread_deg(self) -> float:
        return SPREAD_TRACK_DEG if self.motion_src == "track" else SPREAD_WIND_DEG

    def cone_length_km(self, minutes: int) -> float:
        return min(110.0, max(20.0, (self.speed_kmh or 0) * minutes / 60))

    def cone_half_width(self, along_km: float) -> float:
        return self.radius_km + along_km * math.tan(math.radians(self.spread_deg))


# ---------- кластеризация ----------

def dbscan(xy: np.ndarray, eps: float, min_pts: int) -> np.ndarray:
    """DBSCAN на сетке. Возвращает номер кластера для каждой точки, -1 — шум."""
    n = len(xy)
    labels = np.full(n, -1, dtype=int)
    keys = np.floor(xy / eps).astype(int)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (kx, ky) in enumerate(keys):
        grid[(kx, ky)].append(i)

    def neighbours(i: int) -> np.ndarray:
        kx, ky = keys[i]
        cand = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in grid.get((kx + dx, ky + dy), ())]
        cand = np.array(cand, dtype=int)
        d = xy[cand] - xy[i]
        return cand[(d * d).sum(axis=1) <= eps * eps]

    visited = np.zeros(n, dtype=bool)
    cluster = 0
    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        seeds = neighbours(i)
        if len(seeds) < min_pts:
            continue
        labels[i] = cluster
        queue = list(seeds)
        while queue:
            j = queue.pop()
            if labels[j] == -1:
                labels[j] = cluster
            if visited[j]:
                continue
            visited[j] = True
            nb = neighbours(j)
            if len(nb) >= min_pts:
                queue.extend(k for k in nb if not visited[k])
        cluster += 1
    return labels


def find_clusters(strikes: np.ndarray, cfg: Config) -> list[np.ndarray]:
    """Делит разряды на очаги. Возвращает список массивов (n, 3)."""
    if len(strikes) < cfg.cell_min_strikes:
        return []
    if len(strikes) > MAX_POINTS:
        strikes = strikes[np.argsort(strikes[:, 0])[-MAX_POINTS:]]
    lat0, lon0 = (cfg.lat_min + cfg.lat_max) / 2, (cfg.lon_min + cfg.lon_max) / 2
    x, y = to_xy(strikes[:, 1], strikes[:, 2], lat0, lon0)
    min_pts = max(4, cfg.cell_min_strikes // 2)
    labels = dbscan(np.column_stack([x, y]), cfg.cluster_eps_km, min_pts)
    groups = [strikes[labels == k] for k in range(labels.max() + 1)]
    return [g for g in groups if len(g) >= cfg.cell_min_strikes]


# ---------- движение ----------

def estimate_motion(lat: float, lon: float, radius_km: float, history: np.ndarray, now: float):
    """Скорость (км/ч) и азимут движения очага по смещению разрядов за последние полчаса.

    Разряды рядом с очагом режутся на пятиминутки; через центры пятиминуток проводится прямая.
    Возвращает (speed, bearing), (0.0, None) для малоподвижного очага или None, если данных мало.
    """
    if len(history) < 12:
        return None
    x, y = to_xy(history[:, 1], history[:, 2], lat, lon)
    near = np.hypot(x, y) <= radius_km + 45
    t, x, y = history[near, 0], x[near], y[near]
    bins = np.floor((now - t) / 300).astype(int)
    ts, xs, ys, ws = [], [], [], []
    for b in np.unique(bins):
        m = bins == b
        if m.sum() >= 3:
            ts.append(t[m].mean())
            xs.append(np.median(x[m]))
            ys.append(np.median(y[m]))
            ws.append(math.sqrt(m.sum()))
    if len(ts) < 3 or max(ts) - min(ts) < 9 * 60:
        return None
    ts = (np.array(ts) - now) / 3600.0  # часы
    vx = np.polyfit(ts, xs, 1, w=ws)[0]
    vy = np.polyfit(ts, ys, 1, w=ws)[0]
    speed = float(math.hypot(vx, vy))
    if speed > MAX_SPEED_KMH:
        return None
    if speed < STATIONARY_KMH:
        return 0.0, None
    return speed, math.degrees(math.atan2(vx, vy)) % 360


# ---------- зона прогноза ----------

def circle_polygon(lat: float, lon: float, radius_km: float, step: int = 10) -> list[tuple[float, float]]:
    return [destination(lat, lon, a, radius_km) for a in range(0, 360, step)]


def front_arc(cell: Cell, along_km: float) -> list[tuple[float, float]]:
    """Дуга переднего края грозы, когда центр пройдёт along_km по курсу."""
    centre = destination(cell.lat, cell.lon, cell.bearing, along_km)
    r = cell.cone_half_width(along_km)
    return [destination(*centre, cell.bearing + a, r) for a in range(-90, 91, 10)]


def cone_polygon(cell: Cell, minutes: int) -> list[tuple[float, float]]:
    """Расширяющаяся полоса от круга очага по курсу: куда гроза придёт за minutes минут."""
    left = destination(cell.lat, cell.lon, cell.bearing - 90, cell.radius_km)
    right = destination(cell.lat, cell.lon, cell.bearing + 90, cell.radius_km)
    return [left, *front_arc(cell, cell.cone_length_km(minutes)), right]


def assess_cities(cell: Cell, index: CityIndex, minutes: int) -> None:
    """Заполняет cell.in_circle / in_cone / nearest: какие города под грозой и на её пути."""
    x, y = to_xy(index.lat, index.lon, cell.lat, cell.lon)
    dist = np.hypot(x, y)
    i = int(np.argmin(dist))
    cell.nearest, cell.nearest_km = index.cities[i], float(dist[i])

    inside = dist <= cell.radius_km
    cell.in_circle = [CityHit(index.cities[j], float(dist[j]), None) for j in np.flatnonzero(inside)]
    cell.in_cone = []
    if cell.moving:
        b = math.radians(cell.bearing)
        ux, uy = math.sin(b), math.cos(b)
        along = x * ux + y * uy
        cross = np.abs(x * uy - y * ux)
        length = cell.cone_length_km(minutes)
        # За концом оси зона заканчивается дугой — её радиус равен полуширине на конце.
        beyond = np.clip(along - length, 0, None)
        half = cell.radius_km + np.clip(along, 0, length) * math.tan(math.radians(cell.spread_deg))
        hit = ~inside & (along > 0) & (np.hypot(cross, beyond) <= half) & (cross <= half)
        for j in np.flatnonzero(hit):
            eta = max(0.0, (along[j] - cell.radius_km) / cell.speed_kmh * 60)
            if eta <= minutes:
                cell.in_cone.append(CityHit(index.cities[j], float(dist[j]), eta))
    elif cell.speed_kmh == 0:
        # Малоподвижный очаг: предупреждаем тех, кто вплотную к нему.
        near = ~inside & (dist <= cell.radius_km + 10)
        cell.in_cone = [CityHit(index.cities[j], float(dist[j]), None) for j in np.flatnonzero(near)]
    cell.in_circle.sort(key=lambda h: -h.city.pop)
    cell.in_cone.sort(key=lambda h: (h.eta_min if h.eta_min is not None else 0, -h.city.pop))


# ---------- сопровождение ----------

class StormTracker:
    """Каждый такт заново находит очаги и связывает их с очагами прошлого такта."""

    def __init__(self, cfg: Config, index: CityIndex):
        self.cfg, self.index = cfg, index
        self.cells: dict[int, Cell] = {}
        self._next_id = 1

    def _measure(self, group: np.ndarray, now: float) -> Cell:
        lat0, lon0 = float(group[:, 1].mean()), float(group[:, 2].mean())
        x, y = to_xy(group[:, 1], group[:, 2], lat0, lon0)
        # Центр — медиана (устойчива к одиночным далёким разрядам), радиус накрывает 90 % разрядов.
        cx, cy = float(np.median(x)), float(np.median(y))
        lat, lon = from_xy(cx, cy, lat0, lon0)
        radius = float(np.percentile(np.hypot(x - cx, y - cy), 90)) + 2.0
        radius = min(MAX_RADIUS_KM, max(MIN_RADIUS_KM, radius))
        half = self.cfg.window_min * 60 / 2
        new, old = int((group[:, 0] >= now - half).sum()), int((group[:, 0] < now - half).sum())
        trend = 1 if new > old * 1.5 + 3 else -1 if old > new * 1.5 + 3 else 0
        return Cell(0, lat, lon, radius, len(group), len(group) / self.cfg.window_min, trend, group, now, now)

    def _match(self, cell: Cell, free: dict[int, Cell], now: float) -> Cell | None:
        """Ищет прошлый очаг, который за прошедшее время должен был оказаться здесь."""
        best, best_d = None, float("inf")
        for old in free.values():
            lat, lon = old.lat, old.lon
            if old.moving:
                lat, lon = destination(lat, lon, old.bearing, old.speed_kmh * (now - old.last_seen) / 3600)
            d = float(haversine(cell.lat, cell.lon, lat, lon))
            if d <= max(25.0, cell.radius_km, old.radius_km) and d < best_d:
                best, best_d = old, d
        return best

    def update(self, now_strikes: np.ndarray, history: np.ndarray, now: float | None = None) -> list[Cell]:
        """now_strikes — разряды за window_min, history — за track_min. Возвращает активные очаги."""
        now = now or time.time()
        found = [self._measure(g, now) for g in find_clusters(now_strikes, self.cfg)]
        found.sort(key=lambda c: -c.n)
        free = dict(self.cells)
        current: dict[int, Cell] = {}
        for cell in found:
            old = self._match(cell, free, now)
            if old:
                free.pop(old.id)
                cell.id, cell.first_seen, cell.wind = old.id, old.first_seen, old.wind
            else:
                cell.id, self._next_id = self._next_id, self._next_id + 1

            motion = estimate_motion(cell.lat, cell.lon, cell.radius_km, history, now)
            if motion and motion[1] is not None and old and old.motion_src == "track" and old.moving:
                # Сглаживаем с прошлой оценкой, чтобы стрелка не дёргалась от такта к такту.
                b0, b1 = math.radians(old.bearing), math.radians(motion[1])
                vx = (old.speed_kmh * math.sin(b0) + motion[0] * math.sin(b1)) / 2
                vy = (old.speed_kmh * math.cos(b0) + motion[0] * math.cos(b1)) / 2
                motion = math.hypot(vx, vy), math.degrees(math.atan2(vx, vy)) % 360
            if motion:
                cell.speed_kmh, cell.bearing, cell.motion_src = motion[0], motion[1], "track"
            elif old and old.motion_src == "track":
                cell.speed_kmh, cell.bearing, cell.motion_src = old.speed_kmh, old.bearing, "track"
            elif cell.wind:
                self._apply_wind(cell)
            assess_cities(cell, self.index, self.cfg.forecast_min)
            current[cell.id] = cell

        # Очаги, которые в этом такте не нашлись, ещё немного помним — вдруг разряды возобновятся.
        for old in free.values():
            if now - old.last_seen < LOST_AFTER_SEC:
                current.setdefault(old.id, old)
        self.cells = current
        return [c for c in current.values() if c.last_seen == now]

    def set_wind(self, cell: Cell, speed_kmh: float, bearing_to: float) -> None:
        """Запасная оценка движения по ветру на высоте — пока очаг слишком молод для собственной."""
        cell.wind = (speed_kmh, bearing_to)
        if cell.motion_src != "track":
            self._apply_wind(cell)
            assess_cities(cell, self.index, self.cfg.forecast_min)

    @staticmethod
    def _apply_wind(cell: Cell) -> None:
        speed, brg = cell.wind
        cell.motion_src = "wind"
        cell.speed_kmh, cell.bearing = (speed, brg) if speed >= STATIONARY_KMH else (0.0, None)

    def in_ukraine(self, cell: Cell) -> bool:
        """Очаг над Украиной (рядом есть украинский город) или идёт на украинские города."""
        return cell.nearest_km <= cell.radius_km + 20 or bool(cell.in_cone)
