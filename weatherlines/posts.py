"""Сборка текстов постов о грозах из данных об очагах."""

from html import escape

from .cities import region_name, region_where
from .config import Config
from .geo import bearing, compass8
from .maps import _round_eta
from .storms import Cell
from .texts import fmt_time, plural, tr

MAX_PATH_CITIES = 8


def _names(hits, lang: str, limit: int = 6) -> str:
    return ", ".join(escape(h.city.name(lang)) for h in hits[:limit])


def _key_hits(hits, min_pop: int):
    """Сначала заметные города; если таких нет — все, что есть."""
    return [h for h in hits if h.city.pop >= min_pop] or hits


def storm_post(cell: Cell, cfg: Config, now: float) -> tuple[str, str]:
    """Возвращает (заголовок для карты, текст поста)."""
    lang, t = cfg.language, tr(cfg.language)
    clock = fmt_time(cfg.timezone, now)
    # Очаг ещё за границей, но идёт на украинские города — называем область, куда он придёт.
    incoming = cell.nearest_km > cell.radius_km + 20 and bool(cell.in_cone)
    if incoming:
        region = cell.in_cone[0].city.region
        lines = [t["storm_title_incoming"].format(region=region_name(region, lang, short=False)), ""]
    else:
        region = cell.nearest.region
        lines = [t["storm_title"].format(where=region_where(region, lang)), ""]

    under = _key_hits(cell.in_circle, cfg.alert_city_min_pop // 4)
    if under:
        lines.append(t["storm_now"].format(cities=_names(under, lang)))
    else:
        side = compass8(bearing(cell.nearest.lat, cell.nearest.lon, cell.lat, cell.lon))
        lines.append(t["storm_near"].format(city=escape(cell.nearest.name(lang)), km=max(1, round(cell.nearest_km)),
                                            side=t["dirs_side"][side]))
    trend = t["trend_up"] if cell.trend > 0 else t["trend_down"] if cell.trend < 0 else ""
    lines.append(t["storm_intensity"].format(level=t["levels"][cell.level], n=cell.n, strikes=plural(cell.n, t["strike_forms"]),
                                             min=cfg.window_min, trend=trend))

    if cell.moving:
        key = "storm_moving" if cell.motion_src == "track" else "storm_moving_wind"
        lines.append(t[key].format(dir=t["dirs_to"][compass8(cell.bearing)], speed=_round_speed(cell.speed_kmh)))
    elif cell.speed_kmh == 0:
        lines.append(t["storm_still"])
    else:
        lines.append(t["storm_unknown"])

    ahead = _key_hits(cell.in_cone, cfg.alert_city_min_pop // 4)
    if ahead and cell.moving:
        lines += ["", t["storm_path"].format(min=cfg.forecast_min)]
        for hit in ahead[:MAX_PATH_CITIES]:
            name = escape(hit.city.name(lang))
            if hit.eta_min < 5:
                lines.append(t["storm_path_soon"].format(city=name))
            else:
                lines.append(t["storm_path_item"].format(city=name, eta=_round_eta(hit.eta_min)))
    elif ahead:
        lines += ["", t["storm_close"].format(cities=_names(ahead, lang))]

    lines += ["", t["advice"][cell.level], "", t["storm_footer"].format(time=clock)]
    title = t["storm_title_map"].format(region=region_name(region, lang).upper(), time=clock)
    return title, "\n".join(lines)


def overview_post(cells: list[Cell], cfg: Config, now: float, limit: int = 10) -> tuple[str, str]:
    """Сводка по всем очагам. Порядок cells совпадает с номерами на обзорной карте."""
    lang, t = cfg.language, tr(cfg.language)
    clock = fmt_time(cfg.timezone, now)
    lines = [t["overview_title"].format(time=clock), t["overview_count"].format(n=len(cells)), ""]
    for num, cell in enumerate(cells[:limit], 1):
        if cell.moving:
            motion = t["overview_motion"].format(dir=t["dirs_to"][compass8(cell.bearing)], speed=_round_speed(cell.speed_kmh))
        else:
            motion = t["overview_still"] if cell.speed_kmh == 0 else ""
        ahead = [h for h in cell.in_cone if h.city.pop >= cfg.alert_city_min_pop][:3]
        targets = t["overview_targets"].format(cities=_names(ahead, lang)) if ahead and cell.moving else ""
        under = _key_hits(cell.in_circle, cfg.alert_city_min_pop // 4)
        place = t["overview_place"].format(city=escape((under[0].city if under else cell.nearest).name(lang)))
        lines.append(t["overview_item"].format(num=num, region=region_name(cell.nearest.region, lang),
                                               level=t["levels"][cell.level], place=place, motion=motion, targets=targets))
    if len(cells) > limit:
        lines.append(t["overview_more"].format(n=len(cells) - limit))
    lines += ["", t["storm_footer"].format(time=clock)]
    return t["overview_title_map"].format(time=clock), "\n".join(lines)


def lightning_post(cells: list[Cell], strikes: int, cfg: Config, now: float) -> tuple[str, str]:
    """Карта всех молний в области слежения, включая соседние страны."""
    t = tr(cfg.language)
    clock = fmt_time(cfg.timezone, now)
    text = "\n".join([t["lightning_title"].format(time=clock),
                      t["lightning_count"].format(min=cfg.window_min, n=strikes, cells=len(cells)),
                      "", t["storm_footer"].format(time=clock)])
    return t["lightning_title_map"].format(time=clock), text


def radar_post(strikes: int, cfg: Config, now: float, lightning_min: int) -> tuple[str, str]:
    """Подпись к пиксельной карте радара."""
    t = tr(cfg.language)
    clock = fmt_time(cfg.timezone, now)
    lines = [t["radar_title"].format(time=clock), "", t["radar_body"].format(min=lightning_min)]
    if strikes:
        lines.append(t["radar_stats"].format(min=lightning_min, n=strikes))
    lines += ["", t["radar_note"]]
    return t["radar_title_map"].format(time=clock), "\n".join(lines)


def clear_post(cfg: Config) -> str:
    return tr(cfg.language)["clear"].format(min=cfg.all_clear_min)


def map_strings(cfg: Config) -> dict:
    """Подписи для карт с подставленными числами."""
    s = dict(tr(cfg.language)["map"])
    s["legend_strikes"] = s["legend_strikes"].format(min=cfg.window_min)
    s["legend_cone"] = s["legend_cone"].format(min=cfg.forecast_min)
    return s


def _round_speed(kmh: float) -> int:
    return max(5, int(round(kmh / 5.0)) * 5)
