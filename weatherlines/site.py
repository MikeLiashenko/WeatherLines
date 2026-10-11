"""Сайт WeatherLines: бот складывает в папку site/ страницу, живые данные о грозах и свежие карты
и сам же раздаёт её по HTTP. Папку можно целиком выложить и на любой статический хостинг."""

import json
import logging
import os
import shutil
import time
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

import numpy as np
from aiohttp import web

from . import posts, radar
from .config import SITE_DIR, Config
from .storms import Cell, StormTracker, circle_polygon, cone_polygon
from .texts import WARN_ICONS, fmt_time, tr
from .warnings_feed import Warning, _area_text, warning_type

log = logging.getLogger("site")

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_STRIKES = 4000  # больше точек браузеру на телефоне рисовать тяжело


def _write(path: Path, data: bytes) -> None:
    """Запись через временный файл: посетитель никогда не получит полфайла."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _round(points: list[tuple[float, float]]) -> list[list[float]]:
    return [[round(lat, 4), round(lon, 4)] for lat, lon in points]


class Site:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = SITE_DIR
        self.posts: dict[str, dict] = {}

    def prepare(self) -> None:
        """Раскладывает страницу, стили и скрипт; подставляет в страницу тексты на нужном языке."""
        (self.dir / "data").mkdir(parents=True, exist_ok=True)
        (self.dir / "img").mkdir(exist_ok=True)
        ui = dict(tr(self.cfg.language)["site"])
        channel = self.cfg.channel_id
        ui["channel_url"] = f"https://t.me/{channel[1:]}" if channel.startswith("@") else ""
        ui["window_min"], ui["forecast_min"] = self.cfg.window_min, self.cfg.forecast_min
        ui["tick_sec"] = self.cfg.storm_tick_sec
        ui["timezone"] = self.cfg.timezone
        ui["spotter"] = {"name": self.cfg.spotter_name, "lat": self.cfg.spotter_lat, "lon": self.cfg.spotter_lon,
                         "height": self.cfg.spotter_height}
        page = Template((WEB_DIR / "index.html").read_text(encoding="utf-8")).safe_substitute(
            {**ui, "lang": self.cfg.language, "ui_json": json.dumps(ui, ensure_ascii=False)})
        _write(self.dir / "index.html", page.encode("utf-8"))
        for name in ("style.css", "app.js", "favicon.svg"):
            shutil.copyfile(WEB_DIR / name, self.dir / name)
        try:
            self.posts = json.loads((self.dir / "data" / "posts.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.posts = {}
        if not (self.dir / "data" / "live.json").exists():
            _write(self.dir / "data" / "live.json", b'{"updated": 0, "strikes": [], "cells": [], "warnings": []}')

    def post_age(self, kind: str) -> float:
        """Сколько секунд назад обновлялся раздел (бесконечность, если ещё ни разу)."""
        return time.time() - self.posts[kind]["updated"] if kind in self.posts else float("inf")

    def save_post(self, kind: str, text: str, photo: bytes | None) -> None:
        """Свежая карта с текстом для одного из разделов: rain_now, nowcast, today, tomorrow, synoptic."""
        now = time.time()
        entry = {"updated": now, "time": fmt_time(self.cfg.timezone, now), "text": text,
                 "html": text.replace("\n", "<br>")}
        if photo:
            _write(self.dir / "img" / f"{kind}.jpg", photo)
            entry["image"] = f"img/{kind}.jpg?v={int(now)}"
        self.posts[kind] = entry
        _write(self.dir / "data" / "posts.json", json.dumps(self.posts, ensure_ascii=False).encode("utf-8"))

    def _cell(self, cell: Cell, tracker: StormTracker, now: float) -> dict:
        cfg, t = self.cfg, tr(self.cfg.language)
        home = tracker.in_ukraine(cell)
        item = {
            "id": cell.id, "lat": round(cell.lat, 4), "lon": round(cell.lon, 4), "level": cell.level, "n": cell.n,
            "radius": round(cell.radius_km, 1), "speed": round(cell.speed_kmh or 0, 1),
            "bearing": round(cell.bearing) if cell.moving else None,
            "home": home, "circle": _round(circle_polygon(cell.lat, cell.lon, cell.radius_km)),
            "cone": _round(cone_polygon(cell, cfg.forecast_min)) if cell.moving else None,
        }
        if home:
            _, text = posts.storm_post(cell, cfg, now)
            head, _, rest = text.partition("\n\n")
            item["title"] = head
            item["html"] = rest.rsplit("\n\n", 1)[0].replace("\n", "<br>")  # без строки-подписи с временем
        else:
            item["title"] = t["site"]["cell_abroad"].format(level=t["levels"][cell.level])
            item["html"] = t["site"]["cell_abroad_text"].format(n=cell.n, min=cfg.window_min)
        return item

    def _warning(self, w: Warning) -> dict:
        lang, t = self.cfg.language, tr(self.cfg.language)
        fmt, zone = "%d.%m %H:%M", ZoneInfo(self.cfg.timezone)
        return {
            "icon": WARN_ICONS.get(w.level, "🟡"), "type": warning_type(w, lang),
            "level": t["warn_levels"].get(w.level, t["warn_levels"][2]), "area": _area_text(w, lang),
            "time": f"{w.onset.astimezone(zone).strftime(fmt)} — {w.expires.astimezone(zone).strftime(fmt)}",
            "text": w.desc_uk or w.desc_en,
        }

    def write_live(self, cells: list[Cell], strikes: np.ndarray, warnings: list[Warning], tracker: StormTracker,
                   now: float, recent: int, radar_frames: list | None = None) -> None:
        """Снимок текущей обстановки для живой карты. Вызывается каждый такт слежения.

        radar_frames — кадры радара (время, градации по клеткам); каждый сохраняется крошечной картинкой.
        """
        shots = []
        for i, (when, classes) in enumerate(radar_frames or []):
            _write(self.dir / "img" / f"radar_{i}.png", radar.to_png(classes))
            shots.append({"time": fmt_time(self.cfg.timezone, when), "image": f"img/radar_{i}.png?v={int(now)}"})
        if len(strikes) > MAX_STRIKES:
            strikes = strikes[np.argsort(strikes[:, 0])[-MAX_STRIKES:]]
        cells = sorted(cells, key=lambda c: (not tracker.in_ukraine(c), -c.n))
        live = {
            "updated": now, "time": fmt_time(self.cfg.timezone, now), "recent": recent,
            "strikes": [[round(lat, 3), round(lon, 3), round((now - t) / 60, 1)] for t, lat, lon in strikes.tolist()],
            "cells": [self._cell(c, tracker, now) for c in cells],
            "warnings": [self._warning(w) for w in warnings],
            "radar": {"bounds": radar.bounds(), "frames": shots},
        }
        _write(self.dir / "data" / "live.json", json.dumps(live, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    async def serve(self, setup=None) -> None:
        """Раздаёт папку site/ по HTTP. По умолчанию только с этого компьютера (127.0.0.1).

        setup(app) — способ добавить свои страницы (так подключается админка).
        """
        app = web.Application(client_max_size=12 * 1024 * 1024)

        async def index(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(self.dir / "index.html", headers={"Cache-Control": "no-cache"})

        @web.middleware
        async def no_stale_data(request: web.Request, handler):
            response = await handler(request)
            if request.path.startswith("/data/"):
                response.headers["Cache-Control"] = "no-store"
            return response

        app.middlewares.append(no_stale_data)
        app.router.add_get("/", index)
        if setup:
            setup(app)
        app.router.add_static("/", self.dir)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.cfg.site_host, self.cfg.site_port).start()
        except OSError as exc:
            log.error("сайт не запустился на порту %d: %s", self.cfg.site_port, exc)
            return
        log.info("сайт: http://%s:%d", "localhost" if self.cfg.site_host in ("127.0.0.1", "0.0.0.0") else self.cfg.site_host,
                 self.cfg.site_port)
