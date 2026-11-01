from __future__ import annotations

import logging
import os

import uvicorn

from .cli import ProtonCli
from .db import Db
from .runner import Runner
from .scheduler import Scheduler
from .web import create_app


class _NoHealthz(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


def build():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("uvicorn.access").addFilter(_NoHealthz())
    data = os.environ.get("DATA_DIR", "/data")
    os.makedirs(os.path.join(data, "session"), mode=0o700, exist_ok=True)
    db = Db(os.path.join(data, "state.db"))
    db.mark_stale_runs()
    cli = ProtonCli(session_dir=os.path.join(data, "session"))
    runner = Runner(db, cli)
    sched = Scheduler(db, runner,
                      stale_days=int(os.environ.get("STALE_DAYS", "3")),
                      stale_ping_url=os.environ.get("STALE_PING_URL", ""))
    sched.start()
    return create_app(db, cli, runner)


if __name__ == "__main__":
    uvicorn.run(build(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
