"""Главный цикл бота: слушает молнии, следит за грозами, публикует прогнозы и предупреждения."""

import argparse
import asyncio
import dataclasses
import logging
import ssl
import sys
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

import aiohttp
import certifi

from . import forecast, maps, posts, radar
from .admin import Admin
from .ai import Editor
from .alerts import Action, StormAlerter
from .cities import load_cities
from .config import LOG_FILE, PRECIP_FILE, STRIKES_FILE, VAR_DIR, Config, cfg as default_cfg
from .lightning import BlitzortungClient, StrikeStore
from .site import Site
from .state import load_state, save_state
from .storms import Cell, StormTracker
from .telegram import Telegram
from .texts import local_now
from .warnings_feed import Warning, fetch_warnings, warning_post, warnings_summary

log = logging.getLogger("bot")

SCHEDULE_GRACE = timedelta(minutes=90)  # если бот запустили позже времени поста — насколько поздно ещё публиковать


class Bot:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession):
        self.cfg, self.session = cfg, session
        self.ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self.state = load_state()
        self.index = load_cities()
        self.store = StrikeStore()
        self.tracker = StormTracker(cfg, self.index)
        self.alerter = StormAlerter(cfg, self.tracker, self.state)
        self.lightning = BlitzortungClient(cfg, self.store, session, self.ssl_ctx)
        self.om = forecast.OpenMeteo(cfg, session, self.ssl_ctx)
        self.tiles = maps.TileSource(cfg, session, self.ssl_ctx)
        self.telegram = Telegram(cfg, session, self.ssl_ctx)
        self.editor = Editor(cfg)
        self.strings = posts.map_strings(cfg)
        self.active_warnings: list[Warning] = []
        self.site: Site | None = None  # включается в run(); разовые команды (--once) сайт не трогают
        self.last_cells: list[Cell] = []  # очаги с последнего такта — для админки
        self.precip = radar.load_field(PRECIP_FILE)  # почасовой прогноз осадков — фон для радара

    # ---------- грозы ----------

    async def storm_tick(self, force_overview: bool = False) -> list[Cell]:
        now = time.time()
        self.store.prune(now)
        recent = self.store.recent(self.cfg.window_min, now)
        history = self.store.recent(self.cfg.track_min, now)
        cells = await asyncio.to_thread(self.tracker.update, recent, history, now)
        # Молодой очаг ещё не успел «показать» своё движение — берём ветер на высоте как первую оценку,
        # чтобы направление и конус были сразу (в тексте такое направление помечается как предварительное).
        unknown = [c for c in cells if c.speed_kmh is None and c.wind is None][:50]
        if unknown:
            winds = await self.om.steering([(c.lat, c.lon) for c in unknown])
            for cell, wind in zip(unknown, winds):
                if wind:
                    self.tracker.set_wind(cell, *wind)
        log.info("молний за %d мин: %d, очагов: %d", self.cfg.window_min, len(recent), len(cells))
        if force_overview:
            shown = sorted((c for c in cells if self.tracker.in_ukraine(c)), key=lambda c: -c.n)
            actions = [Action("overview", shown)] if shown else []
        else:
            actions = self.alerter.decide(cells, now)
        for action in actions:
            try:
                await self._publish_storm(action, cells, history, now)
            except Exception:
                log.exception("не удалось опубликовать пост о грозе")
        if actions:
            save_state(self.state)
        self.last_cells = cells
        if self.site:
            try:
                frames = radar.frames(self.precip, self.store.recent(80, now), now)
                self.site.write_live(cells, history, self.active_warnings, self.tracker, now, len(recent), frames)
            except Exception:
                log.exception("не удалось обновить данные сайта")
        return cells

    async def _publish_storm(self, action: Action, cells: list[Cell], strikes, now: float) -> None:
        cfg = self.cfg
        if action.kind == "clear":
            await self.telegram.send(posts.clear_post(cfg), tag="clear")
            return
        if action.kind == "cell":
            cell = action.cells[0]
            title, text = posts.storm_post(cell, cfg, now)
            view = maps.storm_view(cell, cfg.forecast_min)
            others = [c for c in cells if c is not cell]
            render = (maps.render_storm, cell, others, strikes, now, cfg, self.strings, title)
        else:
            title, text = posts.overview_post(action.cells, cfg, now)
            view = maps.overview_view()
            render = (maps.render_overview, action.cells, strikes, now, cfg, self.strings, title)
        photo = await self._render(view, *render)
        await self.telegram.send(text, photo, tag=f"storm_{action.kind}")

    async def _render(self, view: maps.View, func, *args) -> bytes | None:
        """Рисует карту; если не получилось — пост уйдёт без картинки, это лучше, чем не уйти совсем."""
        try:
            base = await self.tiles.basemap(view)
            return await asyncio.to_thread(func, base, view, *args)
        except Exception:
            log.exception("карта не нарисовалась")
            return None

    async def storm_loop(self) -> None:
        restored = self.store.load(STRIKES_FILE, time.time())
        if restored:
            log.info("восстановлено разрядов из прошлого запуска: %d", restored)
        first = True
        while True:
            # После перезапуска с сохранённой историей первый такт делаем сразу, а не через две минуты.
            await asyncio.sleep(10 if first and restored else self.cfg.storm_tick_sec)
            first = False
            try:
                await self.storm_tick()
                self.store.save(STRIKES_FILE)
            except Exception:
                log.exception("сбой в такте слежения за грозами")

    # ---------- официальные предупреждения ----------

    def _wanted(self, w: Warning) -> bool:
        cfg = self.cfg
        if cfg.warnings_land_only and not w.on_land:
            return False
        return w.level >= cfg.warnings_min_level and w.kind not in cfg.warnings_skip_types

    async def check_warnings(self, publish: bool = True) -> list[Warning]:
        self.active_warnings = [w for w in await fetch_warnings(self.session, self.ssl_ctx) if self._wanted(w)]
        seen: dict[str, float] = self.state.setdefault("warnings_seen", {})
        first_run = not self.state.get("warnings_init")
        fresh = [w for w in self.active_warnings if w.key not in seen]
        for w in fresh:
            seen[w.key] = w.expires.timestamp()
        # При самом первом запуске не вываливаем в канал всё, что уже действует, — только запоминаем.
        if publish and not first_run:
            for w in fresh[:5]:
                description = await self.editor.to_russian(w.desc_uk) if self.cfg.language == "ru" else None
                await self.telegram.send(warning_post(w, self.cfg, description), tag=f"warning_{w.kind}")
                await asyncio.sleep(3)
        now = time.time()
        for key in [k for k, expires in seen.items() if expires < now - 2 * 86400]:
            del seen[key]
        self.state["warnings_init"] = True
        save_state(self.state)
        return fresh

    async def warnings_loop(self) -> None:
        while True:
            try:
                fresh = await self.check_warnings()
                log.info("предупреждений действует: %d, новых: %d", len(self.active_warnings), len(fresh))
            except Exception:
                log.exception("не удалось проверить официальные предупреждения")
            await asyncio.sleep(self.cfg.warnings_poll_min * 60)

    # ---------- прогноз и синоптика ----------

    # make_* готовят текст и карту (и заодно обновляют раздел сайта), post_* отправляют их в канал.

    def _to_site(self, kind: str, text: str, photo: bytes | None) -> None:
        if self.site:
            self.site.save_post(kind, text, photo)

    async def make_digest(self, day: int) -> tuple[str, bytes | None]:
        digest = await forecast.build_digest(self.om, self.index, self.cfg, day, warnings_summary(self.active_warnings, self.cfg))
        photo = await self._render(maps.overview_view(), maps.render_forecast, digest.points, self.strings, digest.title)
        self._to_site("today" if day == 0 else "tomorrow", digest.text, photo)
        return digest.text, photo

    async def post_digest(self, day: int) -> None:
        text, photo = await self.make_digest(day)
        await self.telegram.send(await self.editor.polish(text), photo, tag="digest_today" if day == 0 else "digest_tomorrow")

    async def make_synoptic(self) -> tuple[str, bytes | None]:
        field = await self.om.pressure()
        syn = forecast.build_synoptic(field, self.cfg)
        photo = await self._render(maps.synoptic_view(), maps.render_synoptic, field.lats, field.lons, field.now,
                                   syn.lows, syn.highs, self.strings, syn.title)
        self._to_site("synoptic", syn.text, photo)
        return syn.text, photo

    async def post_synoptic(self) -> None:
        text, photo = await self.make_synoptic()
        await self.telegram.send(await self.editor.polish(text), photo, tag="synoptic")

    async def make_precip(self) -> dict[bool, tuple[str, bytes | None]]:
        """Обе карты осадков из одного запроса прогноза: за текущий час (ключ True) и на 3 часа (False)."""
        # Берём с запасом пять часов: первые три идут в карты осадков, все пять — фон для радара до следующего обновления.
        self.precip = await self.om.precipitation(hours=5)
        radar.save_field(self.precip, PRECIP_FILE)
        result = {}
        for current in (True, False):
            part = self.precip.head(1 if current else 3)
            nowcast = forecast.build_nowcast(part, self.index, self.cfg, current)
            levels = forecast.PRECIP_LEVELS_HOUR if current else forecast.PRECIP_LEVELS
            photo = await self._render(maps.overview_view(), maps.render_precip, part, levels, self.strings, nowcast.title)
            self._to_site("rain_now" if current else "nowcast", nowcast.text, photo)
            result[current] = (nowcast.text, photo)
        return result

    async def make_radar(self) -> tuple[str, bytes | None]:
        """«Радар WeatherLines»: пиксельная карта осадков на текущий момент (см. radar.py)."""
        now = time.time()
        if radar._model_rate(self.precip, now) is None:
            try:
                await self.make_precip()  # прогноз устарел или ещё не загружался
            except Exception:
                log.exception("прогноз осадков для радара не загрузился — рисую только грозовые ядра")
        strikes = self.store.recent(radar.LIGHTNING_MIN, now)
        title, text = posts.radar_post(len(strikes), self.cfg, now, radar.LIGHTNING_MIN)
        view = maps.overview_view()
        layer = radar.overlay_for_view(radar.build(self.precip, strikes, now), view)
        photo = await self._render(view, maps.render_radar, layer, radar.LEVELS, radar.COLOURS, self.strings, title)
        return text, photo

    async def post_radar(self) -> None:
        text, photo = await self.make_radar()
        await self.telegram.send(text, photo, tag="radar")

    async def post_nowcast(self, current: bool = False) -> None:
        """Карта осадков: на ближайшие 3 часа или (current=True) за текущий час."""
        text, photo = (await self.make_precip())[current]
        await self.telegram.send(await self.editor.polish(text), photo, tag="rain_now" if current else "nowcast")

    async def draft(self, kind: str, fresh: bool = False) -> tuple[str, bytes | None, str]:
        """Черновик поста для админки: (текст, карта, пометка о свежести данных). Ничего не публикует."""
        cfg, now = self.cfg, time.time()
        if kind == "radar":
            return (*await self.make_radar(), "")
        if kind in ("lightning", "storms"):
            recent = self.store.recent(cfg.window_min, now)
            history = self.store.recent(cfg.track_min, now)
            cells = sorted(self.last_cells, key=lambda c: -c.n)
            if kind == "storms":
                cells = [c for c in cells if self.tracker.in_ukraine(c)]
                if not cells:
                    raise ValueError("Гроз над Украиной сейчас нет — обзор отправлять не из чего.")
                title, text = posts.overview_post(cells, cfg, now)
            else:
                title, text = posts.lightning_post(cells, len(recent), cfg, now)
            photo = await self._render(maps.overview_view(), maps.render_overview, cells, history, now, cfg, self.strings, title)
            return text, photo, ""
        # Прогнозные карты «дорогие» по лимиту запросов, поэтому по умолчанию берём уже готовые с сайта.
        saved = self.site.posts.get(kind) if self.site else None
        if saved and not fresh and now - saved["updated"] < 6 * 3600:
            image = self.site.dir / "img" / f"{kind}.jpg"
            text = saved.get("text") or saved["html"].replace("<br>", "\n")
            return text, image.read_bytes() if image.exists() else None, f"данные от {saved['time']}"
        if kind in ("rain_now", "nowcast"):
            text, photo = (await self.make_precip())[kind == "rain_now"]
        elif kind in ("today", "tomorrow"):
            text, photo = await self.make_digest(0 if kind == "today" else 1)
        else:
            text, photo = await self.make_synoptic()
        return text, photo, "только что пересчитано"

    async def site_loop(self) -> None:
        """Держит разделы сайта свежими независимо от расписания постов в канале."""
        cfg = self.cfg

        async def forecasts() -> None:
            await self.make_digest(0)
            await self.make_digest(1)

        jobs = [("rain_now", cfg.site_precip_min, self.make_precip), ("today", cfg.site_forecast_min, forecasts),
                ("synoptic", cfg.site_synoptic_min, self.make_synoptic)]
        retry_at: dict[str, float] = {}
        await asyncio.sleep(5)  # сначала пусть проверятся предупреждения — они попадают в сводку прогноза
        while True:
            for kind, minutes, job in jobs:
                if self.site.post_age(kind) < minutes * 60 or time.time() < retry_at.get(kind, 0):
                    continue
                try:
                    await job()
                    log.info("сайт: раздел «%s» обновлён", kind)
                except Exception:
                    log.exception("сайт: раздел «%s» не обновился — повторю через 10 минут", kind)
                    retry_at[kind] = time.time() + 600
            await asyncio.sleep(30)

    async def schedule_loop(self) -> None:
        jobs = [("morning", self.cfg.morning_time, lambda: self.post_digest(0)),
                ("synoptic", self.cfg.synoptic_time, self.post_synoptic),
                ("evening", self.cfg.evening_time, lambda: self.post_digest(1))]
        jobs += [(f"nowcast_{at}", at, self.post_nowcast) for at in self.cfg.nowcast_times]
        done: dict[str, str] = self.state.setdefault("jobs", {})
        retry_at: dict[str, float] = {}
        while True:
            now = local_now(self.cfg.timezone)
            today = now.date().isoformat()
            for name, at, job in jobs:
                if not at or done.get(name) == today or time.time() < retry_at.get(name, 0):
                    continue
                due = datetime.combine(now.date(), datetime.strptime(at, "%H:%M").time(), now.tzinfo)
                if not due <= now < due + SCHEDULE_GRACE:
                    continue
                try:
                    await job()
                    done[name] = today
                    save_state(self.state)
                except Exception:
                    log.exception("пост «%s» не вышел — повторю через 5 минут", name)
                    retry_at[name] = time.time() + 300
            await asyncio.sleep(30)

    # ---------- запуск ----------

    async def run(self) -> None:
        cfg = self.cfg
        if cfg.live:
            log.info("Telegram: %s", await self.telegram.check())
        else:
            log.info("пробный режим: посты сохраняются в папку out/ (задайте BOT_TOKEN и CHANNEL_ID в .env)")
        log.info("язык: %s, ИИ-редактор: %s", cfg.language, f"включён ({cfg.ai_model})" if self.editor.enabled else "выключен")
        tasks = [self.lightning.run(), self.storm_loop(), self.schedule_loop()]
        if cfg.warnings_poll_min:
            tasks.append(self.warnings_loop())
        if cfg.site_enabled:
            self.site = Site(cfg)
            self.site.prepare()
            tasks += [self.site.serve(Admin(self).setup), self.site_loop()]
            log.info("админка: http://localhost:%d/admin", cfg.site_port)
        try:
            await asyncio.gather(*tasks)
        finally:
            save_state(self.state)


async def _once(bot: Bot, what: str, minutes: int) -> None:
    """Разовый запуск одной задачи — для проверки."""
    if what == "check":
        print("Telegram:", await bot.telegram.check())
    elif what == "morning":
        await bot.check_warnings(publish=False)
        await bot.post_digest(0)
    elif what == "evening":
        await bot.post_digest(1)
    elif what == "synoptic":
        await bot.post_synoptic()
    elif what == "nowcast":
        await bot.post_nowcast()
    elif what == "rain":
        await bot.post_nowcast(current=True)
    elif what == "radar":
        bot.store.load(STRIKES_FILE, time.time())  # молнии, которые накопил работающий бот
        await bot.post_radar()
    elif what == "warnings":
        bot.state.pop("warnings_seen", None)
        bot.state["warnings_init"] = True
        fresh = await bot.check_warnings()
        print(f"действует предупреждений: {len(bot.active_warnings)}, опубликовано: {min(5, len(fresh))}")
    elif what == "lightning":
        listener = asyncio.create_task(bot.lightning.run())
        print(f"слушаю молнии {minutes} мин…")
        await asyncio.sleep(minutes * 60)
        listener.cancel()
        now = time.time()
        recent = bot.store.recent(bot.cfg.window_min, now)
        cells = sorted(bot.tracker.update(recent, recent, now), key=lambda c: -c.n)
        title, text = posts.lightning_post(cells, len(recent), bot.cfg, now)
        photo = await bot._render(maps.overview_view(), maps.render_overview, cells, recent, now, bot.cfg, bot.strings, title)
        await bot.telegram.send(text, photo, tag="lightning")
        print(text)
    elif what == "storms":
        listener = asyncio.create_task(bot.lightning.run())
        print(f"слушаю молнии {minutes} мин…")
        for _ in range(minutes):
            await asyncio.sleep(60)
            print(f"  разрядов в области слежения: {bot.store.total} (в мире: {bot.lightning.seen_world})")
        listener.cancel()
        cells = await bot.storm_tick(force_overview=True)
        print(f"очагов найдено: {len(cells)}" + ("" if cells else " — гроз сейчас нет, публиковать нечего"))


def _setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")  # консоль Windows иначе спотыкается об эмодзи
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%d.%m %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    file = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    for handler in (console, file):
        handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[console, file])
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _main(args: argparse.Namespace) -> None:
    cfg = dataclasses.replace(default_cfg, dry_run=True) if args.dry_run else default_cfg
    async with aiohttp.ClientSession() as session:
        bot = Bot(cfg, session)
        if args.once:
            await _once(bot, args.once, args.minutes)
        else:
            await bot.run()


def main() -> None:
    parser = argparse.ArgumentParser(prog="weatherlines", description="Погодный бот для Telegram-канала")
    parser.add_argument("--once", choices=["check", "morning", "evening", "synoptic", "warnings", "storms", "lightning", "nowcast", "rain", "radar"],
                        help="выполнить одну задачу и выйти (для проверки)")
    parser.add_argument("--minutes", type=int, default=12, help="сколько минут слушать молнии для --once storms")
    parser.add_argument("--dry-run", action="store_true", help="не постить в Telegram, а сохранять посты в папку out/")
    args = parser.parse_args()
    _setup_logging()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        print("\nбот остановлен")


if __name__ == "__main__":
    main()
