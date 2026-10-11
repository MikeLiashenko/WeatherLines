"""Память бота между перезапусками: что уже опубликовано (чтобы не повторяться)."""

import json
import logging
import os

from .config import STATE_FILE

log = logging.getLogger("state")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("state.json не читается (%s) — начинаю с чистого листа", exc)
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)
