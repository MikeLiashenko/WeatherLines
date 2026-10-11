"""Отправка постов в Telegram-канал. Без токена (или с DRY_RUN=1) посты сохраняются в папку out/."""

import asyncio
import logging
import re
import ssl
import time

import aiohttp

from .config import OUT_DIR, Config

log = logging.getLogger("telegram")

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


def _visible_len(html: str) -> int:
    return len(re.sub(r"<[^>]+>", "", html))


class Telegram:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession, ssl_ctx: ssl.SSLContext):
        self.cfg, self.session, self.ssl_ctx = cfg, session, ssl_ctx

    async def _call(self, method: str, data: aiohttp.FormData | dict) -> dict:
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/{method}"
        for attempt in range(4):
            try:
                kwargs = {"data": data() if callable(data) else data}
                async with self.session.post(url, ssl=self.ssl_ctx, timeout=aiohttp.ClientTimeout(total=60), **kwargs) as resp:
                    body = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("%s: сеть (%s), попытка %d", method, type(exc).__name__, attempt + 1)
                await asyncio.sleep(3 * (attempt + 1))
                continue
            if body.get("ok"):
                return body["result"]
            wait = body.get("parameters", {}).get("retry_after")
            if wait:  # Telegram просит притормозить
                await asyncio.sleep(wait + 1)
                continue
            raise RuntimeError(f"Telegram {method}: {body.get('description', body)}")
        raise RuntimeError(f"Telegram {method}: не удалось отправить")

    async def check(self) -> str:
        """Проверяет токен и доступ к каналу. Возвращает понятное описание."""
        me = await self._call("getMe", {})
        chat = await self._call("getChat", {"chat_id": self.cfg.channel_id})
        return f"бот @{me['username']}, канал «{chat.get('title', chat.get('username', chat['id']))}»"

    async def send(self, text: str, photo: bytes | None = None, tag: str = "post") -> None:
        if not self.cfg.live:
            self._save(text, photo, tag)
            return
        chat = self.cfg.channel_id
        if photo:
            caption = text if _visible_len(text) <= CAPTION_LIMIT else None

            def form() -> aiohttp.FormData:  # форму нельзя переиспользовать при повторе — собираем заново
                f = aiohttp.FormData()
                f.add_field("chat_id", chat)
                f.add_field("photo", photo, filename=f"{tag}.jpg", content_type="image/jpeg")
                if caption:
                    f.add_field("caption", caption)
                    f.add_field("parse_mode", "HTML")
                return f

            await self._call("sendPhoto", form)
            if caption:
                log.info("опубликовано: %s (с картой)", tag)
                return
        await self._call("sendMessage", {"chat_id": chat, "text": text[:TEXT_LIMIT], "parse_mode": "HTML",
                                         "disable_web_page_preview": "true"})
        log.info("опубликовано: %s", tag)

    def _save(self, text: str, photo: bytes | None, tag: str) -> None:
        OUT_DIR.mkdir(exist_ok=True)
        stem = OUT_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}"
        stem.with_suffix(".txt").write_text(text, encoding="utf-8")
        if photo:
            stem.with_suffix(".jpg").write_bytes(photo)
        log.info("пробный режим: пост «%s» сохранён в %s", tag, stem.with_suffix(".txt"))
