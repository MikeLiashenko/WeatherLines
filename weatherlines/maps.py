"""Карты: подложка из тайлов OpenStreetMap и рисование поверх неё (matplotlib)."""

import asyncio
import io
import logging
import math
import ssl
from dataclasses import dataclass

import aiohttp
import numpy as np
from matplotlib import patheffects
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import FancyArrowPatch, Patch, Polygon
from PIL import Image, ImageEnhance

from .config import CACHE_DIR, Config
from .geo import destination, upsample
from .storms import Cell, circle_polygon, cone_polygon, front_arc

log = logging.getLogger("maps")

TILE = 256
BLANK = (236, 236, 230)
HALO = [patheffects.withStroke(linewidth=3, foreground="white")]
DARK_HALO = [patheffects.withStroke(linewidth=3, foreground="#1b1b1b")]

UKRAINE_CENTER = (48.6, 31.3)


def _global_px(lat, lon, zoom: int):
    n = TILE * 2**zoom
    x = (np.asarray(lon, dtype=float) + 180.0) / 360.0 * n
    y = (1.0 - np.arcsinh(np.tan(np.radians(np.asarray(lat, dtype=float)))) / math.pi) / 2.0 * n
    return x, y


@dataclass(frozen=True)
class View:
    """Прямоугольник карты: масштаб и положение левого верхнего угла в «мировых» пикселях."""

    zoom: int
    x0: float
    y0: float
    width: int
    height: int

    def px(self, lat, lon):
        x, y = _global_px(lat, lon, self.zoom)
        return x - self.x0, y - self.y0

    def path(self, points: list[tuple[float, float]]) -> np.ndarray:
        x, y = self.px([p[0] for p in points], [p[1] for p in points])
        return np.column_stack([x, y])

    @staticmethod
    def centered(lat: float, lon: float, zoom: int, width: int, height: int) -> "View":
        x, y = _global_px(lat, lon, zoom)
        return View(zoom, float(x) - width / 2, float(y) - height / 2, width, height)

    @staticmethod
    def fit(points: list[tuple[float, float]], width: int, height: int, pad: float = 0.16,
            min_zoom: int = 5, max_zoom: int = 9) -> "View":
        """Самый крупный масштаб, при котором все точки помещаются в кадр с полями."""
        lats, lons = [p[0] for p in points], [p[1] for p in points]
        for zoom in range(max_zoom, min_zoom - 1, -1):
            x, y = _global_px(lats, lons, zoom)
            if np.ptp(x) <= width * (1 - 2 * pad) and np.ptp(y) <= height * (1 - 2 * pad):
                break
        return View(zoom, float(x.min() + x.max()) / 2 - width / 2, float(y.min() + y.max()) / 2 - height / 2, width, height)


class TileSource:
    """Скачивает тайлы подложки и хранит их на диске, чтобы не дёргать сервер повторно."""

    def __init__(self, cfg: Config, session: aiohttp.ClientSession, ssl_ctx: ssl.SSLContext):
        self.cfg, self.session, self.ssl_ctx = cfg, session, ssl_ctx
        self._limit = asyncio.Semaphore(2)  # правила OSM: не больше двух соединений

    async def _tile(self, z: int, x: int, y: int) -> Image.Image | None:
        if not 0 <= y < 2**z:
            return None
        x %= 2**z
        path = CACHE_DIR / "tiles" / str(z) / str(x) / f"{y}.png"
        if not path.exists():
            url = self.cfg.tile_url.format(z=z, x=x, y=y)
            try:
                async with self._limit, self.session.get(
                    url, ssl=self.ssl_ctx, headers={"User-Agent": self.cfg.user_agent},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
            except Exception as exc:
                log.warning("тайл %s/%s/%s не загрузился: %s", z, x, y, exc or type(exc).__name__)
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        try:
            return Image.open(path).convert("RGB")
        except Exception:
            path.unlink(missing_ok=True)
            return None

    async def basemap(self, view: View) -> Image.Image:
        tx0, ty0 = math.floor(view.x0 / TILE), math.floor(view.y0 / TILE)
        tx1, ty1 = math.floor((view.x0 + view.width) / TILE), math.floor((view.y0 + view.height) / TILE)
        coords = [(x, y) for x in range(tx0, tx1 + 1) for y in range(ty0, ty1 + 1)]
        tiles = await asyncio.gather(*(self._tile(view.zoom, x, y) for x, y in coords))
        img = Image.new("RGB", (view.width, view.height), BLANK)
        for (x, y), tile in zip(coords, tiles):
            if tile:
                img.paste(tile, (round(x * TILE - view.x0), round(y * TILE - view.y0)))
        # Приглушаем подложку, чтобы грозы и подписи читались поверх неё.
        return ImageEnhance.Brightness(ImageEnhance.Color(img).enhance(0.55)).enhance(0.97)


# ---------- общие элементы ----------

def _canvas(base: Image.Image):
    w, h = base.size
    fig = Figure(figsize=(w / 100, h / 100), dpi=100)
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.imshow(base, extent=(0, w, h, 0), interpolation="none")
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)
    ax.axis("off")
    return fig, ax


def _finish(fig, ax, title: str, credit: str) -> bytes:
    w, h = fig.get_size_inches() * fig.dpi
    ax.text(14, 14, title, va="top", ha="left", fontsize=15, fontweight="bold", color="white", zorder=20,
            bbox=dict(boxstyle="round,pad=0.45", facecolor="#14213d", edgecolor="none", alpha=0.92))
    ax.text(w - 8, h - 6, credit, va="bottom", ha="right", fontsize=7.5, color="#222", zorder=20,
            bbox=dict(boxstyle="square,pad=0.25", facecolor="white", edgecolor="none", alpha=0.75))
    buf = io.BytesIO()
    fig.savefig(buf, format="jpg", dpi=100, pil_kwargs={"quality": 90})
    return buf.getvalue()


def _draw_strikes(ax, view: View, strikes: np.ndarray, now: float, window_min: int, scale: float = 1.0) -> None:
    if not len(strikes):
        return
    x, y = view.px(strikes[:, 1], strikes[:, 2])
    age = (now - strikes[:, 0]) / 60
    old, mid, new = age > window_min, (age > window_min / 2) & (age <= window_min), age <= window_min / 2
    ax.scatter(x[old], y[old], s=9 * scale, c="#8e2a1c", alpha=0.45, linewidths=0, zorder=4)
    ax.scatter(x[mid], y[mid], s=20 * scale, c="#ff9100", edgecolors="#5d2f00", linewidths=0.4, zorder=5)
    ax.scatter(x[new], y[new], s=26 * scale, c="#ffee00", edgecolors="#4a3b00", linewidths=0.5, zorder=6)


def _draw_cell(ax, view: View, cell: Cell, minutes: int, marks: dict[int, str] | None = None,
               arrow: bool = True) -> None:
    """Круг «гроза сейчас» и конус «куда идёт». marks — подписи отметок времени на конусе."""
    if cell.moving:
        ax.add_patch(Polygon(view.path(cone_polygon(cell, minutes)), closed=True, zorder=7,
                             facecolor=(1.0, 0.47, 0.0, 0.22), edgecolor="#e65100", linewidth=2, joinstyle="round"))
        length = cell.cone_length_km(minutes)
        for m, label in (marks or {}).items():
            along = cell.speed_kmh * m / 60
            if along > length + 0.5:
                continue
            arc = view.path(front_arc(cell, along))
            if m < minutes:
                ax.plot(arc[:, 0], arc[:, 1], color="#e65100", linewidth=1.4, linestyle=(0, (5, 4)), zorder=8)
            tip = arc[len(arc) // 2]
            ax.text(tip[0], tip[1], label, fontsize=10, fontweight="bold", color="#bf360c", ha="center", va="center",
                    zorder=12, path_effects=HALO)
    ax.add_patch(Polygon(view.path(circle_polygon(cell.lat, cell.lon, cell.radius_km)), closed=True, zorder=9,
                         facecolor=(0.84, 0.0, 0.0, 0.16), edgecolor="#d50000", linewidth=2.6))
    if cell.moving and arrow:
        a = view.path([(cell.lat, cell.lon), destination(cell.lat, cell.lon, cell.bearing, cell.cone_length_km(minutes) * 0.55)])
        ax.add_patch(FancyArrowPatch(a[0], a[1], arrowstyle="-|>", mutation_scale=24, linewidth=3,
                                     color="#7f0000", zorder=10, shrinkA=0, shrinkB=0))


def _legend(ax, strings: dict, with_cone: bool = True) -> None:
    handles = [
        Line2D([], [], marker="o", linestyle="", markersize=7, markerfacecolor="#ffee00", markeredgecolor="#4a3b00",
               label=strings["legend_strikes"]),
        Patch(facecolor=(0.84, 0.0, 0.0, 0.16), edgecolor="#d50000", linewidth=2, label=strings["legend_now"]),
    ]
    if with_cone:
        handles.append(Patch(facecolor=(1.0, 0.47, 0.0, 0.22), edgecolor="#e65100", linewidth=2, label=strings["legend_cone"]))
    ax.legend(handles=handles, loc="lower left", fontsize=10, framealpha=0.9, facecolor="white", edgecolor="#999",
              borderpad=0.7, handlelength=1.6).set_zorder(20)


# ---------- карты гроз ----------

STORM_SIZE = (1200, 900)


def storm_view(cell: Cell, minutes: int) -> View:
    """Кадр вокруг одного очага: круг, конус и запас по краям."""
    pts = circle_polygon(cell.lat, cell.lon, max(cell.radius_km * 1.6, 45), step=45)
    if cell.moving:
        pts += cone_polygon(cell, minutes)
    return View.fit(pts, *STORM_SIZE)


def render_storm(base: Image.Image, view: View, focus: Cell, others: list[Cell], strikes: np.ndarray, now: float,
                 cfg: Config, strings: dict, title: str) -> bytes:
    fig, ax = _canvas(base)
    _draw_strikes(ax, view, strikes, now, cfg.window_min)
    for cell in others:
        _draw_cell(ax, view, cell, cfg.forecast_min)
    marks = {30: strings["mark"].format(30), cfg.forecast_min: strings["mark"].format(cfg.forecast_min)}
    _draw_cell(ax, view, focus, cfg.forecast_min, marks)

    # Города под грозой и на её пути — с ожидаемым временем подхода. Подписи не должны налезать
    # друг на друга: крупные города подписываем первыми, кому места не хватило — остаётся точкой.
    placed: list[tuple[float, float, float, float]] = []
    for hit in sorted(focus.in_circle + focus.in_cone, key=lambda h: -h.city.pop)[:10]:
        x, y = view.px(hit.city.lat, hit.city.lon)
        rows = [hit.city.name(cfg.language)]
        if hit.eta_min is not None:
            rows.append(strings["eta"].format(_round_eta(hit.eta_min)))
        w, h = max(len(r) for r in rows) * 10.5, len(rows) * 18
        for dx, dy in ((9, -7), (-9, -7), (9, 7), (-9, 7)):
            box = (x + dx if dx > 0 else x + dx - w, y + dy - h if dy < 0 else y + dy)
            box = (*box, box[0] + w, box[1] + h)
            fits = 4 < box[0] and box[2] < view.width - 4 and 50 < box[1] and box[3] < view.height - 4
            if fits and not any(box[0] < p[2] and p[0] < box[2] and box[1] < p[3] and p[1] < box[3] for p in placed):
                placed.append(box)
                ax.text(x + dx, y + dy, "\n".join(rows), fontsize=11.5, fontweight="bold", color="#111",
                        ha="left" if dx > 0 else "right", va="bottom" if dy < 0 else "top", linespacing=0.95,
                        zorder=14, path_effects=HALO)
                ax.plot(x, y, "o", markersize=7, markerfacecolor="white", markeredgecolor="#111", markeredgewidth=1.8, zorder=13)
                break
        else:
            ax.plot(x, y, "o", markersize=4.5, markerfacecolor="white", markeredgecolor="#111", markeredgewidth=1.2, zorder=13)
    _legend(ax, strings, with_cone=focus.moving)
    return _finish(fig, ax, title, strings["credit_storm"])


OVERVIEW_SIZE = (1000, 700)


def overview_view() -> View:
    return View.centered(*UKRAINE_CENTER, 6, *OVERVIEW_SIZE)


def render_overview(base: Image.Image, view: View, cells: list[Cell], strikes: np.ndarray, now: float,
                    cfg: Config, strings: dict, title: str) -> bytes:
    """Вся Украина: все очаги с номерами (номера совпадают со списком в тексте поста)."""
    fig, ax = _canvas(base)
    _draw_strikes(ax, view, strikes, now, cfg.window_min, scale=0.4)
    for num, cell in enumerate(cells, 1):
        _draw_cell(ax, view, cell, cfg.forecast_min, arrow=False)
        x, y = view.px(cell.lat, cell.lon)
        ax.text(x, y - 20, str(num), fontsize=10, fontweight="bold", color="white", ha="center", va="center", zorder=15,
                bbox=dict(boxstyle="circle,pad=0.25", facecolor="#b71c1c", edgecolor="white", linewidth=1.2))
    _legend(ax, strings, with_cone=any(c.moving for c in cells))
    return _finish(fig, ax, title, strings["credit_storm"])


def _round_eta(minutes: float) -> int:
    return max(5, int(round(minutes / 5.0)) * 5)


# ---------- синоптическая карта ----------

SYNOPTIC_SIZE = (1000, 760)


def synoptic_view() -> View:
    return View.centered(50.0, 24.0, 4, *SYNOPTIC_SIZE)


def render_synoptic(base: Image.Image, view: View, lats: np.ndarray, lons: np.ndarray, pressure: np.ndarray,
                    lows: list, highs: list, strings: dict, title: str) -> bytes:
    """Изобары, центры циклонов (Н) и антициклонов (В), стрелки прогноза смещения циклонов."""
    fig, ax = _canvas(base)
    # Сетка прогноза грубая — сглаживаем её бикубической интерполяцией, чтобы изобары были плавными.
    fine = upsample(pressure, 6)
    flat = np.linspace(lats[0], lats[-1], fine.shape[0])
    flon = np.linspace(lons[0], lons[-1], fine.shape[1])
    gx, _ = view.px(np.full_like(flon, flat[0]), flon)
    _, gy = view.px(flat, np.full_like(flat, flon[0]))
    levels = np.arange(940, 1072, 4)
    ax.contourf(gx, gy, fine, levels=levels, cmap="RdYlBu_r", alpha=0.30, vmin=985, vmax=1040, zorder=3, antialiased=True)
    lines = ax.contour(gx, gy, fine, levels=levels, colors="#1a237e", linewidths=1.0, zorder=4)
    ax.clabel(lines, levels=levels[::2], fmt="%d", fontsize=8, inline_spacing=2)
    ax.set_xlim(0, view.width)
    ax.set_ylim(view.height, 0)

    for centres, letter, colour in ((highs, strings["high"], "#0d47a1"), (lows, strings["low"], "#c62828")):
        for c in centres:
            x, y = view.px(c.lat, c.lon)
            if not (20 < x < view.width - 20 and 60 < y < view.height - 20):
                continue
            ax.text(x, y, letter, fontsize=30, fontweight="bold", color=colour, ha="center", va="center", zorder=12,
                    path_effects=HALO)
            ax.text(x, y + 27, f"{c.pressure:.0f}", fontsize=11, fontweight="bold", color=colour, ha="center",
                    va="center", zorder=12, path_effects=HALO)
    for c in lows:
        if c.next is not None:
            a = view.path([(c.lat, c.lon), (c.next.lat, c.next.lon)])
            if np.hypot(*(a[1] - a[0])) > 30:
                ax.add_patch(FancyArrowPatch(a[0], a[1], arrowstyle="-|>", mutation_scale=18, linewidth=2.2,
                                             linestyle=(0, (4, 3)), color="#c62828", zorder=11, shrinkA=22, shrinkB=4))
                ax.text(a[1][0], a[1][1] - 12, strings["plus24"], fontsize=9.5, fontweight="bold", color="#c62828",
                        ha="center", va="center", zorder=12, path_effects=HALO)
    return _finish(fig, ax, title, strings["credit_synoptic"])


# ---------- карта прогноза ----------

def _temp_colour(t: float) -> str:
    stops = [(-20, "#4527a0"), (-10, "#1565c0"), (0, "#0097a7"), (8, "#2e7d32"), (16, "#9e9d24"),
             (22, "#ef6c00"), (28, "#d84315"), (34, "#b71c1c")]
    return min(stops, key=lambda s: abs(s[0] - t))[1]


def render_forecast(base: Image.Image, view: View, points: list[tuple[float, float, float, str]],
                    strings: dict, title: str) -> bytes:
    """Плашки с температурой и значком погоды у областных центров. points: (lat, lon, t, значок)."""
    fig, ax = _canvas(base)
    placed: list[tuple[float, float]] = []
    for lat, lon, temp, glyph in points:
        x, y = view.px(lat, lon)
        ax.plot(x, y, "o", markersize=5, color="#111", zorder=9)
        # Плашка над городом; если там уже занято соседом — под ним, справа или слева.
        spots = [(x, y - 18), (x, y + 19), (x + 46, y), (x - 46, y), (x + 46, y - 20), (x - 46, y - 20)]
        bx, by = next((s for s in spots if all(abs(s[0] - p[0]) > 76 or abs(s[1] - p[1]) > 27 for p in placed)), spots[0])
        placed.append((bx, by))
        ax.text(bx, by, f"{glyph} {temp:+.0f}°", fontsize=12.5, fontweight="bold", color="white", ha="center",
                va="center", zorder=10,
                bbox=dict(boxstyle="round,pad=0.3", facecolor=_temp_colour(temp), edgecolor="white", linewidth=1.3))
    return _finish(fig, ax, title, strings["credit_forecast"])


# ---------- карта осадков ----------

PRECIP_COLOURS = ["#aee3ff", "#5db4ff", "#1f78ff", "#0a3fc4", "#7b1fa2", "#e91e63"]


def render_precip(base: Image.Image, view: View, field, levels: tuple[float, ...], strings: dict, title: str) -> bytes:
    """Заливка по сумме осадков за период и значки гроз. field — forecast.PrecipField."""
    fig, ax = _canvas(base)
    lats, lons, grid = field.fine()
    gx, _ = view.px(np.full_like(lons, lats[0]), lons)
    _, gy = view.px(lats, np.full_like(lats, lons[0]))
    ax.contourf(gx, gy, grid, levels=[*levels, 1e6], colors=PRECIP_COLOURS, alpha=0.62, zorder=3, antialiased=True)
    ax.contour(gx, gy, grid, levels=levels[:1], colors="#1f78ff", linewidths=0.8, alpha=0.7, zorder=4)
    ax.set_xlim(0, view.width)
    ax.set_ylim(view.height, 0)

    for i, j in zip(*np.nonzero(field.thunder & (field.total >= levels[0]))):
        x, y = view.px(field.lats[i], field.lons[j])
        if 10 < x < view.width - 10 and 55 < y < view.height - 10:
            ax.text(x, y, "⚡", fontsize=17, color="#ffe600", ha="center", va="center", zorder=8, path_effects=DARK_HALO)

    bounds = [*levels, None]
    handles = [Patch(facecolor=c, edgecolor="#555", linewidth=0.5, alpha=0.8,
                     label=f"{lo:g}–{hi:g}" if hi else f"{lo:g}+")
               for c, lo, hi in zip(PRECIP_COLOURS, bounds, bounds[1:])]
    if field.thunder.any():
        handles.append(Line2D([], [], marker="$⚡$", linestyle="", markersize=11, markerfacecolor="#ffe600",
                              markeredgecolor="#1b1b1b", markeredgewidth=0.6, label=strings["legend_thunder"]))
    ax.legend(handles=handles, title=strings["legend_precip"], loc="lower left", fontsize=10, title_fontsize=10,
              framealpha=0.92, facecolor="white", edgecolor="#999", borderpad=0.7, handlelength=1.6).set_zorder(20)
    return _finish(fig, ax, title, strings["credit_forecast"])


# ---------- радар WeatherLines ----------

def render_radar(base: Image.Image, view: View, layer: Image.Image, levels: tuple[float, ...], colours: tuple[str, ...],
                 strings: dict, title: str) -> bytes:
    """Пиксельная карта осадков: layer — готовый слой клеток (radar.overlay_for_view) поверх подложки."""
    fig, ax = _canvas(Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB"))
    bounds = [*levels, None]
    handles = [Patch(facecolor=c, edgecolor="#333", linewidth=0.5, label=f"{lo:g}–{hi:g}" if hi else f"{lo:g}+")
               for c, lo, hi in zip(colours, bounds, bounds[1:])]
    ax.legend(handles=handles, title=strings["legend_radar"], loc="lower left", fontsize=10, title_fontsize=10,
              framealpha=0.92, facecolor="white", edgecolor="#999", borderpad=0.7, handlelength=1.3,
              handleheight=1.0).set_zorder(20)
    return _finish(fig, ax, title, strings["credit_radar"])
