"""Геометрия на сфере: расстояния, азимуты, локальные проекции."""

import math

import numpy as np

EARTH_R = 6371.0088  # км


def haversine(lat1, lon1, lat2, lon2):
    """Расстояние в км. Работает и с числами, и с массивами numpy."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lon2) - np.radians(lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Азимут из точки 1 в точку 2, градусы по часовой от севера."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def destination(lat: float, lon: float, brg: float, dist_km: float) -> tuple[float, float]:
    """Точка на расстоянии dist_km по азимуту brg."""
    p1, l1, b, d = math.radians(lat), math.radians(lon), math.radians(brg), dist_km / EARTH_R
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def to_xy(lat, lon, lat0: float, lon0: float):
    """Плоские координаты в км относительно (lat0, lon0): x — на восток, y — на север."""
    x = np.radians(np.asarray(lon) - lon0) * EARTH_R * math.cos(math.radians(lat0))
    y = np.radians(np.asarray(lat) - lat0) * EARTH_R
    return x, y


def from_xy(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    lat = lat0 + math.degrees(y / EARTH_R)
    lon = lon0 + math.degrees(x / (EARTH_R * math.cos(math.radians(lat0))))
    return lat, lon


def compass8(brg: float) -> int:
    """Номер румба 0..7: С, СВ, В, ЮВ, Ю, ЮЗ, З, СЗ."""
    return int((brg % 360 + 22.5) // 45) % 8


def upsample(grid: np.ndarray, k: int) -> np.ndarray:
    """Плавно (бикубически) уплотняет регулярную сетку в k раз по каждой оси."""
    from PIL import Image

    rows, cols = grid.shape
    img = Image.fromarray(grid.astype(np.float32), mode="F")
    return np.asarray(img.resize(((cols - 1) * k + 1, (rows - 1) * k + 1), Image.BICUBIC), dtype=float)
