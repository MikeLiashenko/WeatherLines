"""Настройки бота. Значения берутся из файла .env (см. .env.example), остальное — разумные умолчания."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = _str(name)
    return float(raw) if raw else default


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name).lower()
    return default if not raw else raw in ("1", "true", "yes", "on", "да")


def _channel(name: str) -> str:
    """Принимает канал в любом привычном виде: @имя, имя, ссылку https://t.me/имя или числовой id."""
    raw = _str(name)
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):].strip("/")
    if raw and not raw.startswith(("@", "-", "+")) and not raw.isdigit():
        raw = "@" + raw
    return raw


def _list(name: str, default: str) -> tuple[str, ...]:
    return tuple(p.strip().lower() for p in _str(name, default).split(",") if p.strip())


@dataclass(frozen=True)
class Config:
    # --- Telegram ---
    bot_token: str = field(default_factory=lambda: _str("BOT_TOKEN"))
    channel_id: str = field(default_factory=lambda: _channel("CHANNEL_ID"))
    # Без токена бот работает «всухую»: посты складываются в папку out/ вместо канала.
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN"))

    # --- Язык и время ---
    language: str = field(default_factory=lambda: _str("POST_LANGUAGE", "ru").lower())  # ru | uk
    timezone: str = field(default_factory=lambda: _str("TIMEZONE", "Europe/Kyiv"))

    # --- ИИ (необязательно) ---
    anthropic_api_key: str = field(default_factory=lambda: _str("ANTHROPIC_API_KEY"))
    ai_model: str = field(default_factory=lambda: _str("AI_MODEL", "claude-opus-5-5"))

    # --- Грозы ---
    # Область слежения: Украина с запасом ~150 км, чтобы видеть грозы на подходе.
    lat_min: float = 43.5
    lat_max: float = 53.5
    lon_min: float = 20.5
    lon_max: float = 42.0
    storm_tick_sec: int = field(default_factory=lambda: _int("STORM_TICK_SEC", 120))
    window_min: int = 10            # по разрядам за столько минут определяется «где гроза сейчас»
    track_min: int = 30             # по разрядам за столько минут определяется движение
    cluster_eps_km: float = 12.0    # разряды ближе этого расстояния считаются одним очагом
    cell_min_strikes: int = 8       # меньше — шум, очагом не считаем
    alert_min_strikes: int = field(default_factory=lambda: _int("ALERT_MIN_STRIKES", 15))
    forecast_min: int = field(default_factory=lambda: _int("FORECAST_MIN", 60))  # длина «конуса», мин
    alert_city_min_pop: int = field(default_factory=lambda: _int("ALERT_CITY_MIN_POP", 20000))
    rewarn_min: int = field(default_factory=lambda: _int("REWARN_MIN", 45))      # не повторять город чаще
    update_min: int = 20            # не чаще раза в N минут по одному и тому же очагу
    overview_min: int = field(default_factory=lambda: _int("OVERVIEW_MIN", 60))  # обзорная карта не чаще
    max_storm_posts_per_hour: int = field(default_factory=lambda: _int("MAX_STORM_POSTS_PER_HOUR", 8))
    all_clear_min: int = field(default_factory=lambda: _int("ALL_CLEAR_MIN", 45))  # 0 — не писать «отбой»

    # --- Расписание (местное время), пустая строка — не публиковать ---
    morning_time: str = field(default_factory=lambda: _str("MORNING_TIME", "07:30"))
    synoptic_time: str = field(default_factory=lambda: _str("SYNOPTIC_TIME", "09:00"))
    evening_time: str = field(default_factory=lambda: _str("EVENING_TIME", "19:00"))
    # Карта осадков на ближайшие 3 часа: времена через запятую («06:00,12:00,18:00»), пусто — не публиковать.
    # Каждая карта — около 300 обращений к Open-Meteo при дневном лимите 10 000, так что не чаще ~10 раз в сутки.
    nowcast_times: tuple[str, ...] = field(default_factory=lambda: _list("NOWCAST_TIMES", ""))

    # --- Официальные предупреждения Укргидрометцентра (через MeteoAlarm) ---
    warnings_poll_min: int = field(default_factory=lambda: _int("WARNINGS_POLL_MIN", 10))  # 0 — выключить
    warnings_min_level: int = field(default_factory=lambda: _int("WARNINGS_MIN_LEVEL", 2))  # 2 жёлтый, 3 оранж., 4 красный
    warnings_skip_types: tuple[str, ...] = field(default_factory=lambda: _list("WARNINGS_SKIP_TYPES", "coastalevent"))
    # Только предупреждения по областям; чисто морские (порты, лиманы, открытое море) пропускаем.
    warnings_land_only: bool = field(default_factory=lambda: _bool("WARNINGS_LAND_ONLY", True))

    # --- Админка (страница /admin на сайте) ---
    # Без пароля открывается только с этого компьютера; с паролем — с любого устройства, которому виден сайт.
    admin_password: str = field(default_factory=lambda: _str("ADMIN_PASSWORD"))

    # --- Точка съёмки гроз по умолчанию (блок «Съёмка гроз» на сайте) ---
    spotter_name: str = field(default_factory=lambda: _str("SPOTTER_NAME", "Киев"))
    spotter_lat: float = field(default_factory=lambda: _float("SPOTTER_LAT", 50.4501))
    spotter_lon: float = field(default_factory=lambda: _float("SPOTTER_LON", 30.5234))
    spotter_height: float = field(default_factory=lambda: _float("SPOTTER_HEIGHT", 20))  # высота над землёй, м

    # --- Сайт ---
    site_enabled: bool = field(default_factory=lambda: _bool("SITE_ENABLED", True))
    # 127.0.0.1 — сайт виден только с этого компьютера; 0.0.0.0 — всем в локальной сети.
    site_host: str = field(default_factory=lambda: _str("SITE_HOST", "127.0.0.1"))
    site_port: int = field(default_factory=lambda: _int("SITE_PORT", 8765))
    # Как часто обновлять разделы сайта, минут. Осадки — самый «дорогой» раздел по лимиту Open-Meteo.
    site_precip_min: int = field(default_factory=lambda: _int("SITE_PRECIP_MIN", 180))
    site_forecast_min: int = field(default_factory=lambda: _int("SITE_FORECAST_MIN", 180))
    site_synoptic_min: int = field(default_factory=lambda: _int("SITE_SYNOPTIC_MIN", 360))

    # --- Карты ---
    tile_url: str = field(default_factory=lambda: _str("TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"))
    user_agent: str = field(default_factory=lambda: _str("USER_AGENT", "WeatherLinesBot/1.0 (Telegram weather channel bot)"))

    @property
    def live(self) -> bool:
        """Можно ли реально постить в Telegram."""
        return bool(self.bot_token and self.channel_id) and not self.dry_run


cfg = Config()

DATA_DIR = ROOT / "data"
# Всё, что бот пишет сам (память, кэш карт, журнал, пробные посты). На сервере удобно вынести на отдельный том.
VAR_DIR = Path(_str("VAR_DIR") or ROOT)
CACHE_DIR = VAR_DIR / "cache"
OUT_DIR = VAR_DIR / "out"
SITE_DIR = VAR_DIR / "site"
STRIKES_FILE = VAR_DIR / "strikes.json"
PRECIP_FILE = VAR_DIR / "precip.npz"
STATE_FILE = VAR_DIR / "state.json"
LOG_FILE = VAR_DIR / "bot.log"
