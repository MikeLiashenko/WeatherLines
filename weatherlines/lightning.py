"""Поток разрядов молний в реальном времени из сети Blitzortung.org."""

import asyncio
import json
import logging
import os
import ssl
import time
from collections import deque
from pathlib import Path

import aiohttp
import numpy as np

from .config import Config

log = logging.getLogger("lightning")

SERVERS = ("wss://ws1.blitzortung.org/", "wss://ws7.blitzortung.org/", "wss://ws8.blitzortung.org/")
SILENCE_SEC = 90  # мировой поток не молчит так долго — значит, соединение умерло


def _decode(data: str) -> str:
    """Сообщения Blitzortung сжаты вариантом LZW — распаковываем в обычный JSON."""
    table: dict[int, str] = {}
    chars = list(data)
    prev = last = chars[0]
    out = [prev]
    code = 256
    for ch in chars[1:]:
        n = ord(ch)
        cur = ch if n < 256 else table.get(n, last + prev)
        out.append(cur)
        prev = cur[0]
        table[code] = last + prev
        code += 1
        last = cur
    return "".join(out)


class StrikeStore:
    """Разряды за последние полтора часа внутри области слежения."""

    def __init__(self, keep_min: int = 90):
        self._keep = keep_min * 60
        self._items: deque[tuple[float, float, float]] = deque()
        self.total = 0

    def add(self, t: float, lat: float, lon: float) -> None:
        self._items.append((t, lat, lon))
        self.total += 1

    def prune(self, now: float) -> None:
        while self._items and self._items[0][0] < now - self._keep:
            self._items.popleft()

    def recent(self, minutes: float, now: float) -> np.ndarray:
        """Массив (n, 3): время (unix), широта, долгота."""
        since = now - minutes * 60
        rows = [s for s in self._items if since <= s[0] <= now + 5]
        return np.array(rows, dtype=float).reshape(-1, 3)

    # Разряды переживают перезапуск бота: без истории за полчаса нельзя понять, куда движется гроза.

    def save(self, path: Path) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps([[round(t, 1), round(lat, 4), round(lon, 4)] for t, lat, lon in self._items]))
        os.replace(tmp, path)

    def load(self, path: Path, now: float) -> int:
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            return 0
        fresh = [(float(t), float(lat), float(lon)) for t, lat, lon in rows if now - self._keep <= t <= now]
        self._items = deque(sorted(fresh + list(self._items)))
        return len(fresh)


class BlitzortungClient:
    def __init__(self, cfg: Config, store: StrikeStore, session: aiohttp.ClientSession, ssl_ctx: ssl.SSLContext):
        self.cfg, self.store, self.session, self.ssl_ctx = cfg, store, session, ssl_ctx
        self.connected_since: float | None = None
        self.seen_world = 0

    def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(_decode(raw))
            lat, lon = float(msg["lat"]), float(msg["lon"])
            t = float(msg["time"]) / 1e9
        except (ValueError, KeyError, IndexError, TypeError):
            return
        self.seen_world += 1
        c = self.cfg
        if c.lat_min <= lat <= c.lat_max and c.lon_min <= lon <= c.lon_max:
            now = time.time()
            # Часы станций иногда врут — не даём разряду оказаться в будущем или далёком прошлом.
            self.store.add(t if now - 300 <= t <= now else now, lat, lon)

    async def run(self) -> None:
        attempt = 0
        while True:
            url = SERVERS[attempt % len(SERVERS)]
            try:
                async with self.session.ws_connect(url, ssl=self.ssl_ctx, timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                    await ws.send_str('{"a":111}')
                    log.info("подключено к %s", url)
                    self.connected_since = time.time()
                    while True:
                        msg = await asyncio.wait_for(ws.receive(), timeout=SILENCE_SEC)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            attempt = 0
                            self._on_message(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            raise ConnectionError(f"соединение закрыто ({msg.type.name})")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("%s: %s — переподключаюсь", url, exc or type(exc).__name__)
            self.connected_since = None
            attempt += 1
            await asyncio.sleep(min(60, 3 * attempt))
