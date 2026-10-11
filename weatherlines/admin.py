"""Админка: страница /admin, с которой владелец канала вручную отправляет карты и свои сообщения.

Без пароля админка открывается только с этого же компьютера. Чтобы входить с других устройств
(или если сайт выставлен в интернет), задайте ADMIN_PASSWORD в .env.
"""

import asyncio
import hmac
import logging
import secrets
import time
from html import escape
from urllib.parse import urlsplit

from aiohttp import web

from .config import LOG_FILE
from .site import WEB_DIR

log = logging.getLogger("admin")

# Что можно отправить одной кнопкой: ключ → (название, пояснение).
KINDS = {
    "radar": ("Радар WeatherLines", "Пиксельная карта: дождь и грозовые ядра прямо сейчас"),
    "lightning": ("Карта молний", "Все разряды за последние 10 минут — строится мгновенно"),
    "storms": ("Обзор гроз", "Очаги над Украиной с кругами и конусами движения"),
    "rain_now": ("Осадки сейчас", "Оценка осадков за текущий час"),
    "nowcast": ("Осадки на 3 часа", "Где и сколько выпадет в ближайшие три часа"),
    "today": ("Прогноз на сегодня", "Температура и погода по областным центрам"),
    "tomorrow": ("Прогноз на завтра", "То же на завтрашний день"),
    "synoptic": ("Циклоны", "Карта давления, центры циклонов и антициклонов"),
}
TAG_NAMES = {
    "storm_cell": "Гроза (авто)", "storm_overview": "Обзор гроз", "clear": "Отбой грозы", "nowcast": "Осадки на 3 часа",
    "rain_now": "Осадки сейчас", "digest_today": "Прогноз на сегодня", "digest_tomorrow": "Прогноз на завтра",
    "synoptic": "Циклоны", "lightning": "Карта молний", "radar": "Радар", "admin_custom": "Своё сообщение",
}
PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Connecting-IP")
COOKIE = "wl_admin"
MAX_UPLOAD = 12 * 1024 * 1024

LOGIN_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>WeatherLines — вход</title>
<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/admin/admin.css"></head>
<body class="login"><form method="post" action="/admin/login" class="card">
<h1>Админка WeatherLines</h1>{error}
<label>Пароль<input type="password" name="password" autofocus required autocomplete="current-password"></label>
<button class="primary">Войти</button></form></body></html>"""


class Admin:
    def __init__(self, bot):
        self.bot = bot
        self.sessions: set[str] = set()
        self.drafts: dict[str, tuple[float, bytes | None]] = {}  # id → (когда создан, картинка)

    def setup(self, app: web.Application) -> None:
        r = app.router
        r.add_get("/admin", self.page)
        r.add_post("/admin/login", self.login)
        r.add_get(r"/admin/{name:admin\.(?:js|css)}", self.asset)
        r.add_get("/admin/api/status", self.status)
        r.add_post("/admin/api/preview", self.preview)
        r.add_get("/admin/api/draft/{id}.jpg", self.draft_image)
        r.add_post("/admin/api/send", self.send)

    # ---------- доступ ----------

    def _same_origin(self, request: web.Request) -> bool:
        """Запрос на изменение должен прийти с нашей же страницы, а не с чужого сайта, открытого в браузере."""
        origin = request.headers.get("Origin")
        return not origin or urlsplit(origin).netloc == request.host

    def _local(self, request: web.Request) -> bool:
        host = request.host.rsplit(":", 1)[0]
        return (request.remote in ("127.0.0.1", "::1") and host in ("localhost", "127.0.0.1", "[::1]")
                and not any(h in request.headers for h in PROXY_HEADERS))

    def _authorized(self, request: web.Request) -> bool:
        if self.bot.cfg.admin_password:
            return request.cookies.get(COOKIE) in self.sessions
        return self._local(request)

    def _guard(self, request: web.Request) -> None:
        if request.method == "POST" and not self._same_origin(request):
            raise web.HTTPForbidden(text="Запрос с чужого сайта отклонён")
        if not self._authorized(request):
            raise web.HTTPUnauthorized(text="Нет доступа")

    async def page(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            if self.bot.cfg.admin_password:
                return web.Response(text=LOGIN_PAGE.format(error=""), content_type="text/html")
            return web.Response(status=403, content_type="text/html", text=(
                "<meta charset='utf-8'><p>Админка открывается только с компьютера, на котором запущен бот. "
                "Чтобы входить с других устройств, задайте ADMIN_PASSWORD в файле .env и перезапустите бота.</p>"))
        return web.FileResponse(WEB_DIR / "admin.html", headers={"Cache-Control": "no-cache"})

    async def login(self, request: web.Request) -> web.StreamResponse:
        password = self.bot.cfg.admin_password
        if not password or not self._same_origin(request):
            raise web.HTTPForbidden()
        given = str((await request.post()).get("password", ""))
        if not hmac.compare_digest(given.encode(), password.encode()):
            await asyncio.sleep(1.5)  # замедляем перебор
            page = LOGIN_PAGE.format(error='<p class="error">Неверный пароль</p>')
            return web.Response(status=401, text=page, content_type="text/html")
        token = secrets.token_urlsafe(32)
        self.sessions.add(token)
        response = web.HTTPSeeOther("/admin")
        response.set_cookie(COOKIE, token, httponly=True, samesite="Strict", max_age=30 * 86400)
        return response

    async def asset(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_DIR / request.match_info["name"], headers={"Cache-Control": "no-cache"})

    # ---------- данные ----------

    def _recent_posts(self, limit: int = 15) -> list[dict]:
        try:
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-600:]
        except OSError:
            return []
        marks = ("опубликовано: ", "пробный режим: пост «")
        found = []
        for line in lines:
            for mark in marks:
                if mark in line:
                    tag = line.split(mark, 1)[1].split()[0].strip("«»")
                    name = "Предупреждение" if tag.startswith("warning_") else TAG_NAMES.get(tag, tag)
                    found.append({"time": line[:14], "name": name, "trial": mark != marks[0]})
        return found[-limit:][::-1]

    async def status(self, request: web.Request) -> web.Response:
        self._guard(request)
        bot, cfg = self.bot, self.bot.cfg
        now = time.time()
        cells = bot.last_cells
        return web.json_response({
            "live": cfg.live,
            "channel": cfg.channel_id,
            "lightning": bot.lightning.connected_since is not None,
            "strikes": len(bot.store.recent(cfg.window_min, now)),
            "window_min": cfg.window_min,
            "cells_home": sum(bot.tracker.in_ukraine(c) for c in cells),
            "cells_all": len(cells),
            "kinds": [{"key": k, "name": name, "hint": hint} for k, (name, hint) in KINDS.items()],
            "posts": self._recent_posts(),
        })

    async def preview(self, request: web.Request) -> web.Response:
        """Готовит черновик поста: текст можно поправить на странице, картинка хранится здесь до отправки."""
        self._guard(request)
        body = await request.json()
        kind = body.get("kind")
        if kind not in KINDS:
            raise web.HTTPBadRequest(text="Неизвестный тип поста")
        try:
            text, photo, note = await self.bot.draft(kind, fresh=bool(body.get("fresh")))
        except ValueError as exc:  # «гроз сейчас нет» и подобное — не ошибка, а ответ
            return web.json_response({"error": str(exc)}, status=409)
        except Exception as exc:
            log.exception("черновик «%s» не собрался", kind)
            return web.json_response({"error": f"Не получилось подготовить пост: {exc}"}, status=502)
        draft_id = secrets.token_urlsafe(9)
        self.drafts[draft_id] = (time.time(), photo)
        for old in [k for k, (created, _) in self.drafts.items() if time.time() - created > 3600]:
            del self.drafts[old]
        return web.json_response({"id": draft_id, "text": text, "note": note,
                                  "image": f"/admin/api/draft/{draft_id}.jpg" if photo else None})

    async def draft_image(self, request: web.Request) -> web.Response:
        self._guard(request)
        draft = self.drafts.get(request.match_info["id"])
        if not draft or not draft[1]:
            raise web.HTTPNotFound()
        return web.Response(body=draft[1], content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def send(self, request: web.Request) -> web.Response:
        """Отправляет в канал то, что сейчас в редакторе: текст и картинку черновика или загруженный файл."""
        self._guard(request)
        form = await request.post()
        text = str(form.get("text", "")).strip()
        if form.get("html") != "1":
            text = escape(text, quote=False)  # обычный текст: чтобы «<» и «&» не сломали разметку Telegram
        photo = None
        upload = form.get("photo")
        if isinstance(upload, web.FileField):
            photo = upload.file.read()
        elif form.get("draft") in self.drafts:
            photo = self.drafts[str(form.get("draft"))][1]
        if not text and not photo:
            return web.json_response({"error": "Нечего отправлять: нет ни текста, ни картинки"}, status=400)
        kind = str(form.get("kind", ""))
        tag = {"today": "digest_today", "tomorrow": "digest_tomorrow", "storms": "storm_overview"}.get(kind, kind)
        try:
            await self.bot.telegram.send(text, photo, tag=tag if kind in KINDS else "admin_custom")
        except Exception as exc:
            log.warning("отправка из админки не удалась: %s", exc)
            return web.json_response({"error": str(exc)}, status=502)
        self.drafts.pop(str(form.get("draft")), None)
        return web.json_response({"ok": True, "trial": not self.bot.cfg.live})
