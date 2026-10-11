"""Необязательный ИИ-редактор (Claude): оживляет шаблонные тексты и переводит описания предупреждений.

Работает только если в .env задан ANTHROPIC_API_KEY. При любой ошибке бот публикует шаблонный текст.
"""

import logging
import re

import anthropic

from .config import Config

log = logging.getLogger("ai")

# Модели, для которых API умеет сам переключаться на запасную при отказе по соображениям безопасности.
_FALLBACK_PREFIXES = ("claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")

_EDITOR = """Ты редактор Telegram-канала о погоде в Украине. Тебе дают черновик поста, собранный программой \
из данных прогноза. Перепиши его живым, понятным языком — так, как написал бы опытный синоптик для обычных читателей.

Язык поста: {language}.

Что важно:
- Все факты бери только из черновика. Не добавляй чисел, городов, дат, явлений и прогнозов, которых в нём нет, \
и не убирай те, что есть: читатели принимают по этим постам решения, выдуманная деталь хуже сухого текста.
- Первую строку (заголовок) и последнюю строку (источник данных) оставь как есть.
- Разметка — только теги <b> и <i>, как в черновике: другие теги Telegram не примет. Эмодзи можно оставить или \
переставить, но не увлекайся ими.
- Объём — примерно как у черновика; пост читают с телефона.

В ответе — только готовый текст поста, без пояснений."""

_ASSISTANT = """Ты — WeatherLines, бот о погоде и грозах в Украине. Человек написал тебе в личный чат Telegram.

Тебе передают то, что бот знает прямо сейчас: активные грозы, прогнозы по стране, карту давления, официальные \
предупреждения. Ответь на вопрос по этим данным — коротко, по-человечески, до пяти предложений, на языке вопроса.

Что важно:
- Опирайся только на переданные данные. Если в них нет ответа (например, спрашивают про конкретный небольшой город \
или про дату дальше завтрашнего дня), скажи об этом прямо и предложи написать название города — бот пришлёт для \
него подробный прогноз. Люди планируют по твоим словам день, поэтому догадка, поданная как факт, хуже честного «не знаю».
- Вопросы не о погоде вежливо отклоняй одной фразой.
- Пиши обычным текстом, без разметки и без списков."""

_TRANSLATOR = """Переведи текст официального метеорологического предупреждения с украинского на русский. \
Сохрани все числа, единицы измерения и смысл дословно, ничего не добавляй. В ответе — только перевод."""


class Editor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key) if cfg.anthropic_api_key else None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def _ask(self, system: str, text: str) -> str | None:
        extra = {}
        if self.cfg.ai_model.startswith(_FALLBACK_PREFIXES):
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            response = await self.client.beta.messages.create(
                model=self.cfg.ai_model,
                max_tokens=16000,
                system=system,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": text}],
                **extra,
            )
        except anthropic.AuthenticationError:
            log.error("ИИ: неверный ANTHROPIC_API_KEY — публикую шаблонный текст")
            return None
        except anthropic.RateLimitError:
            log.warning("ИИ: превышен лимит запросов — публикую шаблонный текст")
            return None
        except anthropic.APIStatusError as exc:
            log.warning("ИИ: ошибка API %s: %s", exc.status_code, exc.message)
            return None
        except anthropic.APIConnectionError:
            log.warning("ИИ: нет связи с API — публикую шаблонный текст")
            return None
        if response.stop_reason != "end_turn":
            log.warning("ИИ: ответ оборван (%s) — публикую шаблонный текст", response.stop_reason)
            return None
        return "".join(block.text for block in response.content if block.type == "text").strip() or None

    async def polish(self, draft: str) -> str:
        """Возвращает оживлённый текст поста или исходный черновик, если ИИ выключен или ответил странно."""
        if not self.enabled:
            return draft
        language = "украинский" if self.cfg.language == "uk" else "русский"
        text = await self._ask(_EDITOR.format(language=language), draft)
        if not text:
            return draft
        foreign_tags = re.findall(r"</?(?!b>|i>|/b>|/i>)[a-zA-Z][^>]*>", text)
        if foreign_tags or len(text) > len(draft) * 1.6 + 300 or text.count("<b>") != text.count("</b>"):
            log.warning("ИИ: текст не прошёл проверку — публикую шаблонный")
            return draft
        return text

    async def answer(self, question: str, facts: str) -> str | None:
        """Короткий ответ на вопрос о погоде в личном чате — только по переданным данным."""
        if not self.enabled:
            return None
        return await self._ask(_ASSISTANT, f"Данные бота на этот момент:\n\n{facts}\n\nВопрос пользователя: {question}")

    async def to_russian(self, ukrainian: str) -> str | None:
        if not self.enabled or not ukrainian:
            return None
        return await self._ask(_TRANSLATOR, ukrainian)
