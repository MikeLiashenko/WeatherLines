"""Демонстрация без настоящей грозы: рисует придуманные грозы и кладёт посты в папку out/.

    .venv\\Scripts\\python.exe -m weatherlines.demo

Полезно, чтобы посмотреть, как будут выглядеть предупреждения, не дожидаясь лета.
"""

import asyncio
import math
import ssl
import time

import aiohttp
import certifi
import numpy as np

from . import maps, posts
from .cities import load_cities
from .config import OUT_DIR, cfg
from .geo import destination
from .storms import StormTracker


def fake_storm(rng, lat, lon, brg, speed_kmh, now, per_min=9.0, size_km=7.0, minutes=30) -> np.ndarray:
    """Разряды грозы, которая последние minutes минут шла курсом brg и сейчас находится в (lat, lon)."""
    rows = []
    for _ in range(int(per_min * minutes)):
        age = rng.uniform(0, minutes * 60)
        centre = destination(lat, lon, brg + 180, speed_kmh * age / 3600)
        r, a = abs(rng.normal(0, size_km)), rng.uniform(0, 360)
        rows.append((now - age, *destination(*centre, a, r)))
    return np.array(rows)


async def main() -> None:
    rng = np.random.default_rng(7)
    now = time.time()
    strikes = np.vstack([
        fake_storm(rng, 46.25, 30.25, 55, 42, now, per_min=11),           # идёт на Одессу с юго-запада
        fake_storm(rng, 49.55, 25.10, 80, 55, now, per_min=5, size_km=9),  # Тернопольщина, на восток
        fake_storm(rng, 50.95, 33.60, 20, 3, now, per_min=3, size_km=6),   # малоподвижная на Сумщине
        fake_storm(rng, 48.10, 37.20, 110, 30, now, per_min=1.5, size_km=5),
    ])
    index = load_cities()
    tracker = StormTracker(cfg, index)
    recent = strikes[strikes[:, 0] >= now - cfg.window_min * 60]
    cells = [c for c in tracker.update(recent, strikes, now) if tracker.in_ukraine(c)]
    cells.sort(key=lambda c: -c.n)
    strings = posts.map_strings(cfg)
    OUT_DIR.mkdir(exist_ok=True)

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    async with aiohttp.ClientSession() as session:
        tiles = maps.TileSource(cfg, session, ssl_ctx)
        for i, cell in enumerate(cells[:2], 1):
            title, text = posts.storm_post(cell, cfg, now)
            view = maps.storm_view(cell, cfg.forecast_min)
            base = await tiles.basemap(view)
            others = [c for c in cells if c is not cell]
            image = await asyncio.to_thread(maps.render_storm, base, view, cell, others, strikes, now, cfg, strings, title)
            (OUT_DIR / f"demo_storm_{i}.jpg").write_bytes(image)
            (OUT_DIR / f"demo_storm_{i}.txt").write_text(text, encoding="utf-8")
            print(text, "\n" + "-" * 60)

        title, text = posts.overview_post(cells, cfg, now)
        view = maps.overview_view()
        base = await tiles.basemap(view)
        image = await asyncio.to_thread(maps.render_overview, base, view, cells, strikes, now, cfg, strings, title)
        (OUT_DIR / "demo_overview.jpg").write_bytes(image)
        (OUT_DIR / "demo_overview.txt").write_text(text, encoding="utf-8")
        print(text)
    for c in cells:
        speed = "?" if c.speed_kmh is None else f"{c.speed_kmh:.0f}"
        print(f"#{c.id}: {c.n} разр., r={c.radius_km:.0f} км, {speed} км/ч, курс {c.bearing and round(c.bearing)}, {math.floor(c.rate)}/мин")
    print(f"\nКартинки и тексты — в папке {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
