"""Публикация сайта на GitHub Pages: бот раз в несколько минут выкладывает свежую копию папки site/
в отдельную ветку репозитория (по умолчанию gh-pages), а GitHub раздаёт её всем по обычной ссылке.

В ветке всегда один коммит — он перезаписывается, поэтому история не растёт. Админка туда не попадает:
это статическая копия, управлять ботом с неё нельзя.
"""

import asyncio
import logging
import shutil
import time

from .config import PAGES_DIR, Config
from .site import WEB_DIR, Site

log = logging.getLogger("pages")

GIT_TIMEOUT = 120


class Publisher:
    def __init__(self, cfg: Config, site: Site):
        self.cfg, self.site = cfg, site
        self.dir = PAGES_DIR
        self.published = 0

    async def _git(self, *args: str, check: bool = True) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(self.dir), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"git {args[0]}: нет ответа за {GIT_TIMEOUT} с")
        text = out.decode("utf-8", "replace").strip()
        if check and proc.returncode:
            raise RuntimeError(f"git {args[0]}: {text[-300:]}")
        return proc.returncode, text

    async def _init(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if (self.dir / ".git").exists():
            return
        await self._git("init", "-q", "-b", self.cfg.pages_branch)
        await self._git("remote", "add", "origin", self.cfg.pages_remote)
        # Свои имя и почта для этих служебных коммитов: чтобы публикация не зависела от настроек git на машине.
        await self._git("config", "user.name", "WeatherLines bot")
        await self._git("config", "user.email", "weatherlines-bot@users.noreply.github.com")
        await self._git("config", "core.autocrlf", "false")

    def _copy(self) -> None:
        """Свежая копия сайта. Страница собирается заново — с общими настройками вместо личных (см. Site.page)."""
        (self.dir / "index.html").write_text(self.site.page(public=True), encoding="utf-8")
        (self.dir / ".nojekyll").touch()  # отдавать файлы как есть, без обработки Jekyll — так быстрее
        for name in ("style.css", "app.js", "favicon.svg"):
            shutil.copyfile(WEB_DIR / name, self.dir / name)
        for folder in ("data", "img"):
            (self.dir / folder).mkdir(exist_ok=True)
            for src in (self.site.dir / folder).iterdir():
                if src.is_file() and not src.name.endswith(".tmp"):
                    shutil.copyfile(src, self.dir / folder / src.name)

    async def publish(self) -> bool:
        """Выкладывает сайт. Возвращает True, если на GitHub ушла новая версия."""
        await self._init()
        await asyncio.to_thread(self._copy)
        await self._git("add", "-A")
        if not (await self._git("status", "--porcelain"))[1]:
            return False
        has_commit = (await self._git("rev-parse", "--verify", "-q", "HEAD", check=False))[0] == 0
        stamp = time.strftime("%Y-%m-%d %H:%M")
        await self._git("commit", "-q", *(["--amend", "--reset-author"] if has_commit else []), "-m", f"Site update {stamp}")
        await self._git("push", "-q", "--force", "origin", f"HEAD:{self.cfg.pages_branch}")
        self.published += 1
        if self.published % 40 == 0:  # прежние версии файлов больше не нужны — не даём локальной копии разрастаться
            await self._git("gc", "-q", "--prune=now", check=False)
        return True

    async def run(self) -> None:
        log.info("публикация сайта: %s, ветка %s, каждые %d мин", self.cfg.pages_remote, self.cfg.pages_branch,
                 self.cfg.pages_interval_min)
        await asyncio.sleep(45)  # даём боту собрать первые данные
        failures = 0
        while True:
            try:
                await self.publish()
                failures = 0
            except Exception as exc:
                failures += 1
                (log.warning if failures < 5 else log.error)("сайт не опубликовался: %s", exc)
            await asyncio.sleep(self.cfg.pages_interval_min * 60)
