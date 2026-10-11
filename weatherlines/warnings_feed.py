"""Официальные предупреждения Укргидрометцентра из ленты MeteoAlarm (формат CAP)."""

import asyncio
import hashlib
import logging
import re
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import aiohttp

from .cities import region_by_english, region_name
from .config import Config
from .texts import WARN_ICONS, tr

log = logging.getLogger("warnings")

FEED = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-ukraine"
CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"
ALL_UKRAINE = 23  # столько областей в одном предупреждении считаем «всей страной»


@dataclass
class Warning:
    kind: str            # wind, thunderstorm, fog, … (awareness_type из CAP)
    level: int           # 2 жёлтый, 3 оранжевый, 4 красный
    onset: datetime
    expires: datetime
    areas_en: list[str]
    area_uk: str
    event_uk: str
    desc_uk: str
    desc_en: str

    @property
    def on_land(self) -> bool:
        """Касается ли хотя бы одной области (а не только акватории)."""
        return any(region_by_english(a) for a in self.areas_en)

    @property
    def key(self) -> str:
        """Одинаковый для повторных рассылок того же предупреждения — по нему отсекаем дубли."""
        raw = f"{self.kind}|{self.level}|{self.onset.isoformat()}|{self.expires.isoformat()}|{sorted(self.areas_en)}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _param(info: ET.Element, name: str) -> str:
    for p in info.findall(f"{CAP}parameter"):
        if p.findtext(f"{CAP}valueName") == name:
            return p.findtext(f"{CAP}value") or ""
    return ""


def _parse_cap(xml: str) -> list[Warning]:
    root = ET.fromstring(xml)
    if root.findtext(f"{CAP}status") != "Actual" or root.findtext(f"{CAP}msgType") == "Cancel":
        return []
    by_lang: dict[str, list[ET.Element]] = {}
    for info in root.findall(f"{CAP}info"):
        by_lang.setdefault((info.findtext(f"{CAP}language") or "en")[:2].lower(), []).append(info)
    english = by_lang.get("en") or next(iter(by_lang.values()), [])
    ukrainian = by_lang.get("uk", [])
    result = []
    for i, info in enumerate(english):
        local = ukrainian[i] if i < len(ukrainian) else info
        level = _param(info, "awareness_level").split(";")[0].strip()
        kind = _param(info, "awareness_type").split(";")[-1].strip().lower()
        try:
            onset = datetime.fromisoformat(info.findtext(f"{CAP}onset") or info.findtext(f"{CAP}effective"))
            expires = datetime.fromisoformat(info.findtext(f"{CAP}expires"))
        except (TypeError, ValueError):
            continue
        area = info.find(f"{CAP}area")
        area_local = local.find(f"{CAP}area")
        result.append(Warning(
            kind=kind,
            level=int(level) if level.isdigit() else 2,
            onset=onset,
            expires=expires,
            areas_en=[a.strip() for a in (area.findtext(f"{CAP}areaDesc") if area is not None else "").split(",") if a.strip()],
            area_uk=(area_local.findtext(f"{CAP}areaDesc") if area_local is not None else "") or "",
            event_uk=local.findtext(f"{CAP}event") or "",
            desc_uk=(local.findtext(f"{CAP}description") or "").strip(),
            desc_en=(info.findtext(f"{CAP}description") or "").strip(),
        ))
    return result


async def fetch_warnings(session: aiohttp.ClientSession, ssl_ctx: ssl.SSLContext) -> list[Warning]:
    """Все действующие предупреждения по Украине."""
    timeout = aiohttp.ClientTimeout(total=40)
    async with session.get(FEED, ssl=ssl_ctx, timeout=timeout) as resp:
        resp.raise_for_status()
        feed = await resp.text()
    # В ленте одна запись на каждый полигон — нам нужны уникальные ссылки на сами предупреждения.
    links = sorted(set(re.findall(r'type="application/cap\+xml" href="([^"]+)"', feed)))

    async def load(url: str) -> list[Warning]:
        try:
            async with session.get(url, ssl=ssl_ctx, timeout=timeout) as resp:
                resp.raise_for_status()
                return _parse_cap(await resp.text())
        except Exception as exc:
            log.warning("не удалось прочитать %s: %s", url, exc or type(exc).__name__)
            return []

    now = datetime.now().astimezone()
    seen, result = set(), []
    for batch in await asyncio.gather(*(load(u) for u in links[:40])):
        for w in batch:
            if w.expires > now and w.key not in seen:
                seen.add(w.key)
                result.append(w)
    return sorted(result, key=lambda w: (w.onset, -w.level))


def _area_text(w: Warning, lang: str) -> str:
    codes = [region_by_english(a) for a in w.areas_en]
    if not codes or None in codes:
        return w.area_uk or ", ".join(w.areas_en)  # морские районы и прочее — как в оригинале
    if len(set(codes)) >= ALL_UKRAINE:
        return "вся Україна" if lang == "uk" else "вся Украина"
    return ", ".join(region_name(c, lang) for c in dict.fromkeys(codes))


def warning_type(w: Warning, lang: str) -> str:
    t = tr(lang)
    return t["warn_types"].get(w.kind, t["warn_other"])


def warning_post(w: Warning, cfg: Config, description: str | None = None) -> str:
    """Текст поста. description — перевод описания (если есть), иначе берётся украинский оригинал."""
    lang, t = cfg.language, tr(cfg.language)
    zone = ZoneInfo(cfg.timezone)
    fmt = "%d.%m %H:%M"
    lines = [
        t["warn_title"].format(icon=WARN_ICONS.get(w.level, "🟡"), type=warning_type(w, lang)),
        t["warn_level"].format(level=t["warn_levels"].get(w.level, t["warn_levels"][2])),
        t["warn_area"].format(area=escape(_area_text(w, lang))),
        t["warn_time"].format(start=w.onset.astimezone(zone).strftime(fmt), end=w.expires.astimezone(zone).strftime(fmt)),
    ]
    text = description or w.desc_uk or w.desc_en
    if text:
        lines += ["", f"<i>{escape(text)}</i>"]
    lines += ["", t["warn_source"]]
    return "\n".join(lines)


def warnings_summary(items: list[Warning], cfg: Config) -> str:
    """Одна строка для утренней сводки: какие предупреждения сейчас действуют."""
    if not items:
        return ""
    kinds = dict.fromkeys(warning_type(w, cfg.language) for w in sorted(items, key=lambda w: -w.level))
    return tr(cfg.language)["digest_warnings"].format(items=", ".join(kinds))
