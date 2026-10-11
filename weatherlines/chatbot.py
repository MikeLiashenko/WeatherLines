"""Бот в личном чате: меню, прогноз по городу и геолокации, карты по запросу, личные предупреждения о грозе.

В канал бот пишет сам (см. app.py), а здесь — ответы людям, которые открыли с ним диалог.
"""

import asyncio
import difflib
import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from datetime import date
from html import escape

import aiohttp

from . import posts
from .config import ROOT
from .forecast import WX_EMOJI, wx_key
from .geo import bearing, compass8
from .maps import _round_eta
from .state import save_state
from .storms import Cell, threat
from .texts import fmt_temp, fmt_time, plural, tr
from .warnings_feed import warning_post

log = logging.getLogger("chat")

POPULAR = ["Київ", "Харків", "Одеса", "Дніпро", "Львів", "Запоріжжя"]
STOP_WORDS = {"погода", "прогноз", "погоду", "в", "у", "во", "на", "город", "городе", "місто", "місті", "г", "м",
              "какая", "яка", "який", "сейчас", "зараз", "сегодня", "сьогодні", "завтра", "для", "по"}
FAST_TTL = 120        # карты гроз и радара переиспользуем две минуты
SLOW_TTL = 600        # прогнозные карты — десять
POINT_TTL = 20 * 60   # прогноз для точки
MAX_FROM_UKRAINE_KM = 150
NEAR_STORM_KM = 150
ICON = ROOT / "icon.jpg"


def _norm(name: str) -> str:
    name = name.lower().replace("ё", "е")
    name = re.sub(r"['ʼ’`]", "", name)
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", name)).strip()


class ChatBot:
    def __init__(self, bot):
        self.bot, self.cfg, self.tg = bot, bot.cfg, bot.telegram
        self.t = tr(self.cfg.language)
        self.c = self.t["chat"]
        self.subs: dict[str, dict] = bot.state.setdefault("subs", {})  # чат → место, за которым следим
        self.media: dict[str, tuple[float, str, str]] = {}             # карта → (когда, file_id, подпись)
        self.points: dict[tuple, tuple[float, dict]] = {}              # прогноз для точки
        self.last_seen: dict[int, float] = {}
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.tasks: set[asyncio.Task] = set()
        self.names: dict[str, int] = {}
        for i, city in enumerate(bot.index.cities):  # города идут по убыванию населения — тёзок побеждает крупнейший
            for name in (city.uk, city.ru, city.en):
                self.names.setdefault(_norm(name), i)
        c = self.c
        self.menu = {
            "keyboard": [[c["btn_storms"], c["btn_radar"]], [c["btn_rain"], c["btn_forecast"]],
                         [c["btn_synoptic"], c["btn_warnings"]], [{"text": c["btn_location"], "request_location": True}]],
            "resize_keyboard": True, "is_persistent": True,
        }
        self.routes = {
            "/start": self.cmd_start, "/help": self.cmd_help, "/alerts": self.cmd_alerts,
            "/storms": self.cmd_storms, c["btn_storms"]: self.cmd_storms,
            "/radar": self.cmd_radar, c["btn_radar"]: self.cmd_radar,
            "/rain": self.cmd_rain, c["btn_rain"]: self.cmd_rain,
            "/forecast": self.cmd_forecast, c["btn_forecast"]: self.cmd_forecast,
            "/synoptic": self.cmd_synoptic, c["btn_synoptic"]: self.cmd_synoptic,
            "/warnings": self.cmd_warnings, c["btn_warnings"]: self.cmd_warnings,
        }

    # ---------- оформление профиля ----------

    async def setup_profile(self) -> None:
        """Меню команд, описание и аватар бота. Выполняется, только когда что-то из этого изменилось."""
        c = self.c
        channel = self.cfg.channel_id if self.cfg.channel_id.startswith("@") else ""
        profile = {
            "commands": [{"command": name, "description": text} for name, text in c["commands"]],
            "description": c["bot_description"].format(channel=channel),
            "about": c["bot_about"].format(channel=channel),
        }
        stamp = hashlib.sha1(json.dumps(profile, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if self.bot.state.get("profile") != stamp:
            try:
                await self.tg.api("setMyCommands", commands=profile["commands"])
                await self.tg.api("setMyDescription", description=profile["description"])
                await self.tg.api("setMyShortDescription", short_description=profile["about"])
                await self.tg.api("setChatMenuButton", menu_button={"type": "commands"})
                self.bot.state["profile"] = stamp
                log.info("профиль бота обновлён: команды и описание")
            except Exception as exc:
                log.warning("профиль бота не обновился: %s", exc)
        if ICON.exists():
            icon_stamp = f"{ICON.stat().st_size}:{int(ICON.stat().st_mtime)}"
            if self.bot.state.get("profile_icon") != icon_stamp:
                self.bot.state["profile_icon"] = icon_stamp  # одна попытка на файл: метод есть не во всех версиях API
                try:
                    def form() -> aiohttp.FormData:
                        f = aiohttp.FormData()
                        f.add_field("photo", json.dumps({"type": "static", "photo": "attach://icon"}))
                        f.add_field("icon", ICON.read_bytes(), filename="icon.jpg", content_type="image/jpeg")
                        return f

                    await self.tg._call("setMyProfilePhoto", form)
                    log.info("аватар бота установлен из icon.jpg")
                except Exception as exc:
                    log.warning("аватар бота не установился (%s) — его можно задать через @BotFather", exc)
        save_state(self.bot.state)

    # ---------- приём сообщений ----------

    async def run(self) -> None:
        await self.setup_profile()
        offset = None
        while True:
            try:
                updates = await self.tg.api("getUpdates", offset=offset, timeout=25,
                                            allowed_updates=["message", "callback_query"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("не удалось получить сообщения: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                task = asyncio.create_task(self._safe(update))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    async def _safe(self, update: dict) -> None:
        chat = (update.get("message") or update.get("callback_query", {}).get("message") or {}).get("chat", {})
        try:
            await self.dispatch(update)
        except Exception as exc:
            if "blocked" in str(exc) or "deactivated" in str(exc):
                return
            log.exception("сбой при ответе в чате")
            try:
                await self.tg.reply(chat["id"], self.c["error"])
            except Exception:
                pass

    async def dispatch(self, update: dict) -> None:
        if query := update.get("callback_query"):
            await self.tg.api("answerCallbackQuery", callback_query_id=query["id"])
            chat = query.get("message", {}).get("chat", {})
            if chat.get("type") == "private":
                await self.on_button(chat["id"], query.get("data", ""))
            return
        message = update.get("message") or {}
        chat = message.get("chat", {})
        if chat.get("type") != "private":
            return  # в группах и каналах бот не разговаривает
        now = time.time()
        if now - self.last_seen.get(chat["id"], 0) < 0.7:
            return  # защита от «пулемёта»
        self.last_seen[chat["id"]] = now
        if place := message.get("location"):
            await self.send_place(chat["id"], place["latitude"], place["longitude"])
        elif text := (message.get("text") or "").strip():
            await self.on_text(chat["id"], text)

    async def on_text(self, chat: int, text: str) -> None:
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else text
        if handler := self.routes.get(command):
            await handler(chat)
            return
        if text.startswith("/"):
            await self.cmd_help(chat)
            return
        index, guesses = self.find_city(text)
        if index is not None:
            city = self.bot.index.cities[index]
            await self.send_place(chat, city.lat, city.lon, index)
        elif guesses:
            rows = [[{"text": self.bot.index.cities[i].name(self.cfg.language), "callback_data": f"p:{i}"}] for i in guesses]
            await self.tg.reply(chat, self.c["city_guess"], keyboard={"inline_keyboard": rows})
        else:
            await self.answer_freely(chat, text)

    async def on_button(self, chat: int, data: str) -> None:
        kind, _, arg = data.partition(":")
        if kind == "p" and arg.isdigit() and int(arg) < len(self.bot.index.cities):
            city = self.bot.index.cities[int(arg)]
            await self.send_place(chat, city.lat, city.lon, int(arg))
        elif kind == "l":
            lat, lon = map(float, arg.split(":"))
            await self.send_place(chat, lat, lon)
        elif kind in ("s", "sl"):
            if kind == "s":
                city = self.bot.index.cities[int(arg)]
                lat, lon, name = city.lat, city.lon, city.name(self.cfg.language)
            else:
                lat, lon = map(float, arg.split(":"))
                name = self.place_name(lat, lon)
            self.subs[str(chat)] = {"lat": lat, "lon": lon, "name": name, "last": 0}
            save_state(self.bot.state)
            await self.tg.reply(chat, self.c["subscribed"].format(name=escape(name)))
        elif kind == "u":
            self.subs.pop(str(chat), None)
            save_state(self.bot.state)
            await self.tg.reply(chat, self.c["unsubscribed"])
        elif kind == "k":
            await self.send_map(chat, arg, self.rain_buttons() if arg in ("rain_now", "nowcast") else None)

    # ---------- команды ----------

    async def cmd_start(self, chat: int) -> None:
        await self.tg.reply(chat, self.c["start"], keyboard=self.menu)

    async def cmd_help(self, chat: int) -> None:
        await self.tg.reply(chat, self.c["help"], keyboard=self.menu)

    async def cmd_storms(self, chat: int) -> None:
        await self.send_map(chat, "storms")

    async def cmd_radar(self, chat: int) -> None:
        await self.send_map(chat, "radar")

    async def cmd_rain(self, chat: int) -> None:
        await self.send_map(chat, "nowcast", self.rain_buttons())

    async def cmd_synoptic(self, chat: int) -> None:
        await self.send_map(chat, "synoptic")

    async def cmd_forecast(self, chat: int) -> None:
        cities = [(self.names[_norm(n)], n) for n in POPULAR if _norm(n) in self.names]
        buttons = [{"text": self.bot.index.cities[i].name(self.cfg.language), "callback_data": f"p:{i}"} for i, _ in cities]
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        rows.append([{"text": self.c["digest_today"], "callback_data": "k:today"}])
        rows.append([{"text": self.c["digest_tomorrow"], "callback_data": "k:tomorrow"}])
        await self.tg.reply(chat, self.c["ask_city"], keyboard={"inline_keyboard": rows})

    async def cmd_warnings(self, chat: int) -> None:
        active = self.bot.active_warnings
        if not active:
            await self.tg.reply(chat, self.c["no_warnings"])
            return
        parts = [self.c["warnings_head"]]
        parts += [warning_post(w, self.cfg).rsplit("\n\n", 1)[0] for w in active[:6]]  # без строки «Источник» у каждого
        await self.tg.reply(chat, "\n\n".join(parts))

    async def cmd_alerts(self, chat: int) -> None:
        sub = self.subs.get(str(chat))
        if not sub:
            await self.tg.reply(chat, self.c["alerts_off"])
            return
        keyboard = {"inline_keyboard": [[{"text": self.c["sub_off"], "callback_data": "u"}]]}
        await self.tg.reply(chat, self.c["alerts_on"].format(name=escape(sub["name"])), keyboard=keyboard)

    def rain_buttons(self) -> dict:
        return {"inline_keyboard": [[{"text": self.c["rain_now"], "callback_data": "k:rain_now"},
                                     {"text": self.c["rain_3h"], "callback_data": "k:nowcast"}]]}

    # ---------- карты ----------

    async def send_map(self, chat: int, kind: str, keyboard: dict | None = None) -> None:
        """Карта по запросу. Один и тот же снимок уходит всем, кто попросил в ближайшие пару минут."""
        if kind not in ("storms", "radar", "lightning", "rain_now", "nowcast", "today", "tomorrow", "synoptic"):
            return
        ttl = FAST_TTL if kind in ("storms", "radar", "lightning") else SLOW_TTL
        async with self.locks[kind]:
            cached = self.media.get(kind)
            if cached and time.time() - cached[0] < ttl:
                await self.tg.reply(chat, cached[2], cached[1], keyboard)
                return
            await self.tg.api("sendChatAction", chat_id=chat, action="upload_photo")
            try:
                text, photo, _ = await self.bot.draft(kind)
            except ValueError:  # гроз нет
                now = time.time()
                count = len(self.bot.store.recent(self.cfg.window_min, now))
                await self.tg.reply(chat, self.c["no_storms"].format(min=self.cfg.window_min, n=count))
                return
            sent = await self.tg.reply(chat, text, photo, keyboard)
            if photo and sent.get("photo"):
                self.media[kind] = (time.time(), sent["photo"][-1]["file_id"], text)

    # ---------- прогноз для места ----------

    def find_city(self, query: str) -> tuple[int | None, list[int]]:
        """Ищет город по названию на любом из трёх языков. Возвращает (номер города, варианты-подсказки)."""
        words = [w for w in _norm(query).split() if w not in STOP_WORDS]
        name = " ".join(words)
        if len(name) < 2:
            return None, []
        if name in self.names:
            return self.names[name], []
        if len(name) >= 4:
            starts = sorted({i for known, i in self.names.items() if known.startswith(name)})
            if starts:
                return starts[0], []
        close = difflib.get_close_matches(name, self.names, n=4, cutoff=0.74)
        found = list(dict.fromkeys(self.names[k] for k in close))
        if close and difflib.SequenceMatcher(None, name, close[0]).ratio() >= 0.82:
            return found[0], []  # опечатка или падеж: «в Одессе», «Харкив»
        return None, found[:3]

    def place_name(self, lat: float, lon: float) -> str:
        city, km = self.bot.index.nearest(lat, lon)
        name = city.name(self.cfg.language)
        return name if km <= 12 else self.c["place_near"].format(name=name)

    async def point(self, lat: float, lon: float) -> dict:
        key = (round(lat, 2), round(lon, 2))
        cached = self.points.get(key)
        if cached and time.time() - cached[0] < POINT_TTL:
            return cached[1]
        data = await self.bot.om.point(lat, lon)
        self.points[key] = (time.time(), data)
        for old in [k for k, (ts, _) in self.points.items() if time.time() - ts > POINT_TTL]:
            del self.points[old]
        return data

    def storm_line(self, lat: float, lon: float) -> str:
        """Одна строка о грозах рядом с точкой."""
        c, t = self.c, self.t
        best: tuple[int, float, Cell] | None = None  # (важность, минуты или км, очаг)
        for cell in self.bot.last_cells:
            status, value = threat(cell, lat, lon, self.cfg.forecast_min)
            rank = {"over": 0, "coming": 1, "far": 2}[status]
            if status == "far" and value > NEAR_STORM_KM:
                continue
            if best is None or (rank, value) < (best[0], best[1]):
                best = (rank, value, cell)
        if best is None:
            return c["storm_none"]
        rank, value, cell = best
        if rank == 0:
            return c["storm_over"]
        if rank == 1:
            direction = t["dirs_to"][compass8(cell.bearing)]
            if value < 5:
                return c["storm_coming_soon"].format(level=t["levels"][cell.level], dir=direction)
            return c["storm_coming"].format(eta=_round_eta(value), level=t["levels"][cell.level], dir=direction,
                                            speed=posts._round_speed(cell.speed_kmh))
        side = t["dirs_side"][compass8(bearing(lat, lon, cell.lat, cell.lon))]
        return c["storm_near"].format(km=max(1, round(value)), side=side)

    def format_place(self, name: str, data: dict, lat: float, lon: float) -> str:
        c, t = self.c, self.t
        now, hourly, daily = data["current"], data["hourly"], data["daily"]
        key = wx_key(int(now["weather_code"] or 0))
        lines = [
            c["place_now"].format(name=escape(name), time=fmt_time(self.cfg.timezone)),
            c["now_line"].format(emoji=WX_EMOJI[key], temp=fmt_temp(now["temperature_2m"]),
                                 feels=fmt_temp(now["apparent_temperature"]), sky=t["wx"][key]),
            c["now_details"].format(wind=round(now["wind_speed_10m"]), gust=round(now["wind_gusts_10m"]),
                                    humidity=round(now["relative_humidity_2m"]), pressure=round(now["pressure_msl"])),
            "", self.storm_line(lat, lon), "", c["hours_head"],
        ]
        hour = now["time"][:13]
        start = next((i for i, stamp in enumerate(hourly["time"]) if stamp.startswith(hour)), 0)
        for i in range(start + 1, min(start + 11, len(hourly["time"])), 3):
            wet = hourly["precipitation"][i] or 0
            lines.append(c["hour_line"].format(
                time=hourly["time"][i][11:16], temp=fmt_temp(hourly["temperature_2m"][i]),
                emoji=WX_EMOJI[wx_key(int(hourly["weather_code"][i] or 0))],
                rain=c["hour_rain"].format(mm=f"{wet:.1f}".rstrip("0").rstrip(".")) if wet >= 0.1 else ""))
        lines += ["", c["days_head"]]
        for d, stamp in enumerate(daily["time"][:3]):
            day = date.fromisoformat(stamp)
            label = c["day_names"][d] if d < 2 else f"{c['weekdays'][day.weekday()].capitalize()} {day:%d.%m}"
            key = wx_key(int(daily["weather_code"][d] or 0))
            wet, gust = daily["precipitation_sum"][d] or 0, daily["wind_gusts_10m_max"][d] or 0
            lines.append(c["day_line"].format(
                day=label, tmin=fmt_temp(daily["temperature_2m_min"][d])[:-1], tmax=fmt_temp(daily["temperature_2m_max"][d]),
                emoji=WX_EMOJI[key], sky=t["wx"][key],
                rain=c["day_rain"].format(mm=round(wet)) if wet >= 0.5 else "",
                wind=c["day_wind"].format(gust=round(gust)) if gust >= 12 else ""))
        return "\n".join(lines)

    async def send_place(self, chat: int, lat: float, lon: float, city: int | None = None) -> None:
        """Погода для города (city — его номер в списке) или для произвольной точки."""
        if city is None:
            if self.bot.index.nearest(lat, lon)[1] > MAX_FROM_UKRAINE_KM:
                await self.tg.reply(chat, self.c["outside"])
                return
            name, place, subscribe = self.place_name(lat, lon), f"l:{lat:.3f}:{lon:.3f}", f"sl:{lat:.3f}:{lon:.3f}"
        else:
            name, place, subscribe = self.bot.index.cities[city].name(self.cfg.language), f"p:{city}", f"s:{city}"
        await self.tg.api("sendChatAction", chat_id=chat, action="typing")
        text = self.format_place(name, await self.point(lat, lon), lat, lon)
        watching = self.subs.get(str(chat), {}).get("name") == name
        alert = ({"text": self.c["sub_off"], "callback_data": "u"} if watching
                 else {"text": self.c["sub_on"], "callback_data": subscribe})
        keyboard = {"inline_keyboard": [[alert], [{"text": self.c["refresh"], "callback_data": place},
                                                  {"text": self.c["map_storms"], "callback_data": "k:storms"}]]}
        await self.tg.reply(chat, text, keyboard=keyboard)

    # ---------- свободные вопросы (если подключён ИИ) ----------

    async def answer_freely(self, chat: int, text: str) -> None:
        editor = self.bot.editor
        if not editor.enabled:
            await self.tg.reply(chat, self.c["city_unknown"].format(query=escape(text[:60])))
            return
        await self.tg.api("sendChatAction", chat_id=chat, action="typing")
        cells = sorted((c for c in self.bot.last_cells if self.bot.tracker.in_ukraine(c)), key=lambda c: -c.n)
        facts = [f"Время: {fmt_time(self.cfg.timezone)} (Киев)."]
        facts.append(posts.overview_post(cells, self.cfg, time.time())[1] if cells else "Гроз над Украиной сейчас нет.")
        saved = self.bot.site.posts if self.bot.site else {}
        facts += [saved[k]["text"] for k in ("rain_now", "nowcast", "today", "tomorrow", "synoptic") if saved.get(k, {}).get("text")]
        facts += [warning_post(w, self.cfg) for w in self.bot.active_warnings[:5]]
        answer = await editor.answer(text[:500], re.sub(r"<[^>]+>", "", "\n\n".join(facts)))
        await self.tg.reply(chat, escape(answer) if answer else self.c["ai_fail"])

    # ---------- личные предупреждения ----------

    async def notify(self, cells: list[Cell], now: float) -> None:
        """Пишет подписчикам, на чьи места идёт гроза. Вызывается каждый такт слежения."""
        if not self.subs:
            return
        c, t, cfg = self.c, self.t, self.cfg
        strong = [cell for cell in cells if cell.n >= max(cfg.cell_min_strikes, 10)]
        changed = False
        for chat, sub in list(self.subs.items()):
            if now - sub.get("last", 0) < cfg.rewarn_min * 60:
                continue
            hits = [(threat(cell, sub["lat"], sub["lon"], cfg.forecast_min), cell) for cell in strong]
            hits = [(status, value, cell) for (status, value), cell in hits if status != "far"]
            if not hits:
                continue
            status, value, cell = min(hits, key=lambda h: (h[0] != "over", h[1]))
            level = t["levels"][cell.level].capitalize()
            if status == "over" or value < 5:
                text = c["alert_over"].format(name=escape(sub["name"]), level_cap=level, n=cell.n, min=cfg.window_min,
                                              strikes=plural(cell.n, t["strike_forms"]), advice=t["advice"][cell.level])
            else:
                text = c["alert_coming"].format(name=escape(sub["name"]), eta=_round_eta(value), level_cap=level,
                                                dir=t["dirs_to"][compass8(cell.bearing)],
                                                speed=posts._round_speed(cell.speed_kmh), advice=t["advice"][cell.level])
            keyboard = {"inline_keyboard": [[{"text": c["map_storms"], "callback_data": "k:storms"}]]}
            try:
                await self.tg.reply(int(chat), text, keyboard=keyboard)
                sub["last"] = now
            except Exception as exc:
                if "blocked" in str(exc) or "deactivated" in str(exc) or "chat not found" in str(exc):
                    del self.subs[chat]  # человек остановил бота — больше не пишем
                else:
                    log.warning("личное предупреждение не доставлено: %s", exc)
            changed = True
        if changed:
            save_state(self.bot.state)
