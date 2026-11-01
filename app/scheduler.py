from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from croniter import croniter

from .db import Db
from .runner import Runner, ping

log = logging.getLogger("scheduler")


class Scheduler:
    def __init__(self, db: Db, runner: Runner, stale_days: int = 3, stale_ping_url: str = ""):
        self.db, self.runner = db, runner
        self.stale_days, self.stale_ping_url = stale_days, stale_ping_url
        self._last_checked = time.time()
        self._stop = threading.Event()

    def due(self, now: float) -> list[int]:
        out = []
        for j in self.db.jobs():
            if not j["enabled"]:
                continue
            nxt = croniter(j["cron"], datetime.fromtimestamp(self._last_checked)).get_next(float)
            if nxt <= now:
                out.append(j["id"])
        return out

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        for job_id in self.due(now):
            self.runner.run_job(job_id)
        self._last_checked = now
        self._staleness(now)

    def _staleness(self, now: float) -> None:
        if not self.stale_ping_url or not self.db.jobs():
            return
        last = self.db.last_success()
        if last is not None and now - last > self.stale_days * 86400:
            log.error("no successful backup in %d days", self.stale_days)
            ping(self.stale_ping_url, False, f"no successful backup in {self.stale_days} days", "staleness")

    def loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")

    def start(self) -> None:
        threading.Thread(target=self.loop, daemon=True, name="scheduler").start()

    def stop(self) -> None:
        self._stop.set()
