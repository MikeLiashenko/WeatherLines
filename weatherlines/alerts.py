"""Решает, о каких грозах и когда писать в канал, чтобы предупреждать вовремя и не спамить."""

import time
from collections import deque
from dataclasses import dataclass

from .config import Config
from .storms import Cell, StormTracker


@dataclass
class Action:
    kind: str                  # cell — пост об одном очаге, overview — обзорная карта, clear — «отбой»
    cells: list[Cell]


class StormAlerter:
    def __init__(self, cfg: Config, tracker: StormTracker, state: dict):
        self.cfg, self.tracker = cfg, tracker
        # Когда какой город уже предупреждали — переживает перезапуск бота.
        self.city_warned: dict[str, float] = state.setdefault("city_warned", {})
        self.cell_posted: dict[int, tuple[float, int]] = {}  # очаг → (время поста, уровень на тот момент)
        self.post_times: deque[float] = deque()
        self.last_overview = 0.0
        self.last_activity = 0.0
        self.episode_open = False  # были посты о грозах, «отбой» ещё не дан

    def _fresh_targets(self, cell: Cell, now: float) -> list:
        """Заметные города под грозой или на её пути, которых давно не предупреждали."""
        quiet = self.cfg.rewarn_min * 60
        hits = [h for h in cell.in_circle + cell.in_cone if h.city.pop >= self.cfg.alert_city_min_pop]
        return [h for h in hits if now - self.city_warned.get(h.city.key, 0) > quiet]

    def _wants_post(self, cell: Cell, now: float) -> bool:
        posted = self.cell_posted.get(cell.id)
        fresh = self._fresh_targets(cell, now)
        if posted is None:
            # Новый очаг: пишем, если под ударом заметный город или гроза сильная сама по себе.
            if cell.motion_src != "track" and now - cell.first_seen < 5 * 60:
                return False  # дадим пару тактов измерить движение по самим разрядам
            if fresh:
                return True
            quiet = now - self.city_warned.get(cell.nearest.key, 0) > self.cfg.rewarn_min * 60
            return cell.level >= 2 and quiet
        last_ts, last_level = posted
        if now - last_ts < self.cfg.update_min * 60:
            return False
        return bool(fresh) or cell.level > last_level

    def _mark(self, cells: list[Cell], now: float) -> None:
        for cell in cells:
            self.cell_posted[cell.id] = (now, cell.level)
            self.city_warned[cell.nearest.key] = now
            for hit in cell.in_circle + cell.in_cone:
                self.city_warned[hit.city.key] = now

    def decide(self, cells: list[Cell], now: float | None = None) -> list[Action]:
        """cells — активные очаги с последнего такта. Возвращает, что нужно опубликовать."""
        now = now or time.time()
        cfg = self.cfg
        relevant = [c for c in cells if c.n >= cfg.alert_min_strikes and self.tracker.in_ukraine(c)]
        relevant.sort(key=lambda c: -c.n)
        while self.post_times and now - self.post_times[0] > 3600:
            self.post_times.popleft()
        for key in [k for k, ts in self.city_warned.items() if now - ts > 6 * 3600]:
            del self.city_warned[key]

        if not relevant:
            quiet_for = now - self.last_activity
            if self.episode_open and cfg.all_clear_min and quiet_for >= cfg.all_clear_min * 60:
                self.episode_open = False
                self.cell_posted.clear()
                return [Action("clear", [])]
            return []
        self.last_activity = now

        wanting = [c for c in relevant if self._wants_post(c, now)]
        budget = cfg.max_storm_posts_per_hour - len(self.post_times)
        # Обзор по таймеру — для гроз, о которых отдельных постов не было (слабые, вдали от городов):
        # такой очаг должен прожить хотя бы 10 минут, а канал — молчать о грозах не меньше 20.
        last_post = self.post_times[-1] if self.post_times else 0.0
        unreported = [c for c in relevant if c.id not in self.cell_posted and now - c.first_seen >= 10 * 60]
        overview_due = bool(unreported) and now - self.last_overview >= cfg.overview_min * 60 and now - last_post >= 20 * 60
        actions: list[Action] = []
        if wanting and len(wanting) <= 2 and budget >= len(wanting):
            actions = [Action("cell", [c]) for c in wanting]
            self._mark(wanting, now)
        elif (wanting and now - self.last_overview >= 20 * 60) or overview_due:
            # Очагов много сразу (или лимит постов исчерпан) — одна обзорная карта вместо пачки сообщений.
            actions = [Action("overview", relevant)]
            self._mark(relevant, now)
            self.last_overview = now
        for _ in actions:
            self.post_times.append(now)
        if actions:
            self.episode_open = True
        return actions
