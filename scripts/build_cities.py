"""Собирает data/cities.json — города и посёлки Украины с координатами, населением и областью.

Источник: OpenStreetMap через Overpass API. Запускать нужно редко (файл уже лежит в репозитории):

    .venv\\Scripts\\python.exe scripts\\build_cities.py
"""

import json
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import certifi

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Для каждого региона (admin_level=4) выводим сначала сам регион, затем его города.
QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="UA"][admin_level=2]->.ua;
rel(area.ua)["boundary"="administrative"]["admin_level"="4"]["ISO3166-2"];
foreach->.r(
  .r out tags;
  .r map_to_area->.a;
  node(area.a)["place"~"^(city|town)$"];
  out;
);
"""

OUT = Path(__file__).resolve().parent.parent / "data" / "cities.json"


def fetch() -> dict:
    ctx = ssl.create_default_context(cafile=certifi.where())
    data = urllib.parse.urlencode({"data": QUERY}).encode()
    last_error = None
    for url in OVERPASS_URLS:
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "WeatherLinesBot/0.1"})
            with urllib.request.urlopen(req, timeout=240, context=ctx) as resp:
                return json.load(resp)
        except Exception as exc:  # пробуем следующее зеркало
            last_error = exc
            print(f"{url}: {exc}", file=sys.stderr)
    raise SystemExit(f"Overpass недоступен: {last_error}")


def population(tags: dict) -> int:
    raw = "".join(ch for ch in tags.get("population", "") if ch.isdigit())
    return int(raw) if raw else 0


def main() -> None:
    elements = fetch()["elements"]
    cities, region = {}, None
    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "relation":
            region = tags["ISO3166-2"]
            continue
        name_uk = tags.get("name:uk") or tags.get("name")
        if not name_uk or region is None:
            continue
        cities[el["id"]] = {
            "uk": name_uk,
            "ru": tags.get("name:ru") or name_uk,
            "en": tags.get("name:en") or tags.get("int_name") or name_uk,
            "lat": round(el["lat"], 4),
            "lon": round(el["lon"], 4),
            "pop": population(tags),
            "place": tags["place"],
            "region": region,
        }
    result = sorted(cities.values(), key=lambda c: -c["pop"])
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=0), encoding="utf-8")
    regions = sorted({c["region"] for c in result})
    print(f"{len(result)} населённых пунктов, {len(regions)} регионов: {' '.join(regions)}")


if __name__ == "__main__":
    main()
