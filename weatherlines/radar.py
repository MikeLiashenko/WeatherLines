"""Радар WeatherLines — собственная пиксельная карта осадков.

Это расчётная карта, а не данные метеорадара. Она складывается из двух источников:
  * осадки по прогнозной модели (Open-Meteo) — фон: где и насколько сильно идёт дождь;
  * разряды молний за последние 15 минут — грозовые ядра: где молнии, там самые сильные осадки.
Каждая клетка сетки — один «пиксель» размером около 13 км.
"""

import io
import math
from pathlib import Path

import numpy as np
from PIL import Image

from . import maps
from .forecast import PrecipField
from .geo import upsample

CELL = 8                 # пиксель радара = 8×8 точек обзорной карты (масштаб 6)
LIGHTNING_MIN = 15       # по разрядам за столько минут рисуются грозовые ядра
FRAME_STEP_MIN = 10      # шаг кадров анимации
FRAME_COUNT = 7          # кадров в анимации: последний час

# Градации интенсивности, мм/ч, и их цвета (классическая радарная шкала).
LEVELS = (0.1, 0.5, 1.5, 3.0, 6.0, 12.0, 25.0)
COLOURS = ("#2a9df4", "#23c552", "#0f8f3a", "#ffe600", "#ff9100", "#ff2d2d", "#d81bff")
# Плотность разрядов в клетке → не ниже какой градации там осадки (молнии бьют из самых мощных облаков).
LIGHTNING_CLASSES = ((0.25, 4), (1.0, 5), (3.0, 6), (8.0, 7))
SPREAD = np.array([[0.25, 0.5, 0.25], [0.5, 1.0, 0.5], [0.25, 0.5, 0.25]])


def _grid() -> tuple[int, int, int, int]:
    """Сетка радара в клетках: левый верхний угол (колонка, строка) и размер. Совпадает с кадром обзорной карты."""
    view = maps.overview_view()
    c0, r0 = math.floor(view.x0 / CELL), math.floor(view.y0 / CELL)
    return c0, r0, math.ceil((view.x0 + view.width) / CELL) - c0, math.ceil((view.y0 + view.height) / CELL) - r0


def _lon(col: float) -> float:
    return col * CELL / (maps.TILE * 2**6) * 360.0 - 180.0


def _lat(row: float) -> float:
    y = 1.0 - 2.0 * row * CELL / (maps.TILE * 2**6)
    return math.degrees(math.atan(math.sinh(math.pi * y)))


def bounds() -> list[list[float]]:
    """Углы карты радара для сайта: [[юг, запад], [север, восток]]."""
    c0, r0, cols, rows = _grid()
    return [[round(_lat(r0 + rows), 5), round(_lon(c0), 5)], [round(_lat(r0), 5), round(_lon(c0 + cols), 5)]]


def _model_rate(field: PrecipField | None, when: float) -> np.ndarray | None:
    """Интенсивность осадков по модели на момент when в клетках радара, мм/ч."""
    if field is None or field.hourly is None:
        return None
    # Значение часа относится к его середине; между соседними часами плавно переходим.
    pos = (when - field.start) / 3600.0 - 0.5
    if pos < -2.0 or pos > len(field.hourly) - 0.5:
        return None  # прогноз этот момент не покрывает
    i = int(np.clip(math.floor(pos), 0, len(field.hourly) - 1))
    j = min(i + 1, len(field.hourly) - 1)
    frac = float(np.clip(pos - i, 0.0, 1.0))
    coarse = field.hourly[i] * (1 - frac) + field.hourly[j] * frac
    k = 8
    fine = np.clip(upsample(coarse, k), 0, None)
    lats = np.linspace(field.lats[0], field.lats[-1], fine.shape[0])
    lons = np.linspace(field.lons[0], field.lons[-1], fine.shape[1])
    c0, r0, cols, rows = _grid()
    cell_lat = np.array([_lat(r0 + r + 0.5) for r in range(rows)])
    cell_lon = np.array([_lon(c0 + c + 0.5) for c in range(cols)])
    ii = np.clip(np.searchsorted(lats, cell_lat), 0, len(lats) - 1)
    jj = np.clip(np.searchsorted(lons, cell_lon), 0, len(lons) - 1)
    return fine[np.ix_(ii, jj)]


def _lightning_density(strikes: np.ndarray, when: float) -> np.ndarray:
    """Сколько разрядов (с учётом давности) пришлось на клетку и её соседей к моменту when."""
    c0, r0, cols, rows = _grid()
    density = np.zeros((rows, cols))
    if len(strikes):
        age = (when - strikes[:, 0]) / 60.0
        recent = (age >= 0) & (age <= LIGHTNING_MIN)
        gx, gy = maps._global_px(strikes[recent, 1], strikes[recent, 2], 6)
        col = np.floor(gx / CELL).astype(int) - c0
        row = np.floor(gy / CELL).astype(int) - r0
        inside = (col >= 0) & (col < cols) & (row >= 0) & (row < rows)
        weight = 1.0 - 0.6 * age[recent][inside] / LIGHTNING_MIN  # свежие разряды весомее
        np.add.at(density, (row[inside], col[inside]), weight)
    # Гроза шире точки удара — размазываем на соседние клетки.
    padded = np.pad(density, 1)
    return sum(SPREAD[a, b] * padded[a:a + rows, b:b + cols] for a in range(3) for b in range(3))


def build(field: PrecipField | None, strikes: np.ndarray, when: float) -> np.ndarray:
    """Кадр радара на момент when: массив (строки, колонки) с номером градации 0..7 (0 — сухо)."""
    density = _lightning_density(strikes, when)
    classes = np.zeros(density.shape, dtype=np.uint8)
    rate = _model_rate(field, when)
    if rate is not None:
        classes = np.searchsorted(LEVELS, rate, side="right").astype(np.uint8)
    for threshold, level in LIGHTNING_CLASSES:
        classes = np.where(density >= threshold, np.maximum(classes, level), classes)
    return classes.astype(np.uint8)


def frames(field: PrecipField | None, strikes: np.ndarray, now: float) -> list[tuple[float, np.ndarray]]:
    """Кадры за последний час с шагом 10 минут, последний — «сейчас»."""
    times = [now - FRAME_STEP_MIN * 60 * k for k in range(FRAME_COUNT - 1, -1, -1)]
    return [(t, build(field, strikes, t)) for t in times]


def to_rgba(classes: np.ndarray, alpha: int = 255) -> Image.Image:
    """Картинка «клетка = пиксель» с прозрачным фоном."""
    palette = np.zeros((len(COLOURS) + 1, 4), dtype=np.uint8)
    for n, colour in enumerate(COLOURS, 1):
        palette[n] = (*bytes.fromhex(colour[1:]), alpha)
    return Image.fromarray(palette[classes], mode="RGBA")


def to_png(classes: np.ndarray) -> bytes:
    buf = io.BytesIO()
    to_rgba(classes).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def overlay_for_view(classes: np.ndarray, view: maps.View, alpha: int = 215) -> Image.Image:
    """Слой радара размером с обзорную карту: каждая клетка — чёткий квадрат 8×8 точек."""
    c0, r0, cols, rows = _grid()
    blocks = to_rgba(classes, alpha).resize((cols * CELL, rows * CELL), Image.NEAREST)
    layer = Image.new("RGBA", (view.width, view.height), (0, 0, 0, 0))
    layer.paste(blocks, (round(c0 * CELL - view.x0), round(r0 * CELL - view.y0)))
    return layer


# Поле осадков сохраняем на диск: после перезапуска бота радар сразу знает прогноз, не тратя лимит запросов.

def save_field(field: PrecipField, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp.npz")
    np.savez_compressed(tmp, lats=field.lats, lons=field.lons, hourly=field.hourly,
                        thunder=field.hourly_thunder, start=field.start)
    tmp.replace(path)


def load_field(path: Path) -> PrecipField | None:
    try:
        with np.load(path) as data:
            return PrecipField.from_hours(data["lats"], data["lons"], data["hourly"], data["thunder"], float(data["start"]))
    except (OSError, ValueError, KeyError):
        return None
