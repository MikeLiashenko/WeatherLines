"""Города и области Украины: названия на двух языках и поиск ближайших."""

import json
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .config import DATA_DIR
from .geo import haversine


@dataclass(frozen=True)
class City:
    uk: str
    ru: str
    en: str
    lat: float
    lon: float
    pop: int
    region: str

    def name(self, lang: str) -> str:
        return self.uk if lang == "uk" else self.ru

    @property
    def key(self) -> str:
        return f"{self.uk}|{self.region}"


# Код ISO → (прилагательное укр. им., укр. местн., рус. им., рус. предл., англ. корень для MeteoAlarm)
_OBLASTS = {
    "UA-05": ("Вінницька", "Вінницькій", "Винницкая", "Винницкой", "vinnyt"),
    "UA-07": ("Волинська", "Волинській", "Волынская", "Волынской", "volyn"),
    "UA-09": ("Луганська", "Луганській", "Луганская", "Луганской", "luhansk"),
    "UA-12": ("Дніпропетровська", "Дніпропетровській", "Днепропетровская", "Днепропетровской", "dnipro"),
    "UA-14": ("Донецька", "Донецькій", "Донецкая", "Донецкой", "donetsk"),
    "UA-18": ("Житомирська", "Житомирській", "Житомирская", "Житомирской", "zhytomyr"),
    "UA-21": ("Закарпатська", "Закарпатській", "Закарпатская", "Закарпатской", "zakarpat"),
    "UA-23": ("Запорізька", "Запорізькій", "Запорожская", "Запорожской", "zaporizh"),
    "UA-26": ("Івано-Франківська", "Івано-Франківській", "Ивано-Франковская", "Ивано-Франковской", "ivano"),
    "UA-32": ("Київська", "Київській", "Киевская", "Киевской", "kyiv"),
    "UA-35": ("Кіровоградська", "Кіровоградській", "Кировоградская", "Кировоградской", "kirovohrad"),
    "UA-46": ("Львівська", "Львівській", "Львовская", "Львовской", "lviv"),
    "UA-48": ("Миколаївська", "Миколаївській", "Николаевская", "Николаевской", "mykolaiv"),
    "UA-51": ("Одеська", "Одеській", "Одесская", "Одесской", "odes"),
    "UA-53": ("Полтавська", "Полтавській", "Полтавская", "Полтавской", "poltava"),
    "UA-56": ("Рівненська", "Рівненській", "Ровенская", "Ровенской", "rivne"),
    "UA-59": ("Сумська", "Сумській", "Сумская", "Сумской", "sumy"),
    "UA-61": ("Тернопільська", "Тернопільській", "Тернопольская", "Тернопольской", "ternopil"),
    "UA-63": ("Харківська", "Харківській", "Харьковская", "Харьковской", "kharkiv"),
    "UA-65": ("Херсонська", "Херсонській", "Херсонская", "Херсонской", "kherson"),
    "UA-68": ("Хмельницька", "Хмельницькій", "Хмельницкая", "Хмельницкой", "khmelnyt"),
    "UA-71": ("Черкаська", "Черкаській", "Черкасская", "Черкасской", "cherkas"),
    "UA-74": ("Чернігівська", "Чернігівській", "Черниговская", "Черниговской", "chernihiv"),
    "UA-77": ("Чернівецька", "Чернівецькій", "Черновицкая", "Черновицкой", "chernivtsi"),
}
# Регионы, которые не «область»: (укр. им., укр. «де», рус. им., рус. «где», англ. корень)
_SPECIAL = {
    "UA-30": ("Київ", "у Києві", "Киев", "в Киеве", "kyiv city"),
    "UA-40": ("Севастополь", "у Севастополі", "Севастополь", "в Севастополе", "sevastopol"),
    "UA-43": ("Крим", "у Криму", "Крым", "в Крыму", "crimea"),
}


def region_name(code: str, lang: str, short: bool = True) -> str:
    """«Одесская обл.» / «Одеська обл.» (или полное «…область»)."""
    if code in _SPECIAL:
        return _SPECIAL[code][0 if lang == "uk" else 2]
    adj = _OBLASTS[code][0 if lang == "uk" else 2]
    return f"{adj} обл." if short else f"{adj} область"


def region_where(code: str, lang: str) -> str:
    """«в Одесской области» / «в Одеській області»."""
    if code in _SPECIAL:
        return _SPECIAL[code][1 if lang == "uk" else 3]
    if lang == "uk":
        adj = _OBLASTS[code][1]
        prep = "в" if adj[0] in "АЕЄИІЇОУЮЯ" else "у"
        return f"{prep} {adj} області"
    adj = _OBLASTS[code][3]
    prep = "во" if adj.startswith("Льв") else "в"
    return f"{prep} {adj} области"


def region_by_english(name: str) -> str | None:
    """Код области по английскому названию из ленты MeteoAlarm («Lviv oblast»)."""
    low = name.lower().strip()
    if "city" in low and "kyiv" in low:
        return "UA-30"
    for code, row in _SPECIAL.items():
        if code != "UA-30" and row[4] in low:
            return code
    if "oblast" not in low and "region" not in low:
        return None
    for code, row in _OBLASTS.items():
        if low.startswith(row[4]):
            return code
    return None


class CityIndex:
    def __init__(self, cities: list[City]):
        self.cities = cities
        self.lat = np.array([c.lat for c in cities])
        self.lon = np.array([c.lon for c in cities])
        self.pop = np.array([c.pop for c in cities])

    def nearest(self, lat: float, lon: float, min_pop: int = 0) -> tuple[City, float]:
        """Ближайший населённый пункт (не меньше min_pop жителей) и расстояние до него в км."""
        dist = haversine(lat, lon, self.lat, self.lon)
        if min_pop:
            dist = np.where(self.pop >= min_pop, dist, np.inf)
        i = int(np.argmin(dist))
        return self.cities[i], float(dist[i])

    def by_name(self, uk_name: str) -> City | None:
        # Список отсортирован по населению, поэтому из тёзок вернётся самый крупный.
        return next((c for c in self.cities if c.uk == uk_name), None)


@lru_cache(maxsize=1)
def load_cities() -> CityIndex:
    raw = json.loads((DATA_DIR / "cities.json").read_text(encoding="utf-8"))
    return CityIndex([City(c["uk"], c["ru"], c["en"], c["lat"], c["lon"], c["pop"], c["region"]) for c in raw])


# Областные центры для сводок прогноза, сгруппированные по частям страны (укр. названия как в cities.json).
MACRO_REGIONS = {
    "west": ["Львів", "Ужгород", "Івано-Франківськ", "Тернопіль", "Чернівці", "Луцьк", "Рівне", "Хмельницький"],
    "north": ["Київ", "Житомир", "Чернігів", "Суми"],
    "center": ["Вінниця", "Черкаси", "Кропивницький", "Полтава", "Дніпро"],
    "south": ["Одеса", "Миколаїв", "Херсон", "Запоріжжя", "Сімферополь"],
    "east": ["Харків", "Донецьк", "Луганськ"],
}
