from __future__ import annotations

import logging
import os

import uvicorn

from .cli import ProtonCli
from .cli_install import Installer
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
    os.makedirs(os.path.join(data, "home"), mode=0o700, exist_ok=True)  # HOME for the CLI in the container
    db = Db(os.path.join(data, "state.db"))
    db.mark_stale_runs()
    # The CLI lives in the data folder unless PROTON_CLI_BIN points elsewhere (e.g. a mounted binary).
    cli_path = os.environ.get("PROTON_CLI_BIN") or os.path.join(data, "bin", "proton-drive")
    cli = ProtonCli(binary=cli_path, session_dir=os.path.join(data, "session"))
    installer = Installer(cli_path, version=os.environ.get("PROTON_CLI_VERSION") or None,
                          enabled=os.environ.get("PROTON_CLI_AUTO_DOWNLOAD", "1") != "0")
    installer.on_ready = cli.invalidate_status
    installer.start()
    saved = db.get_setting("upload_workers")  # a value saved in the UI wins over UPLOAD_WORKERS
    if saved:
        cli.set_upload_workers(int(saved))
    runner = Runner(db, cli)
    sched = Scheduler(db, runner,
                      stale_days=int(os.environ.get("STALE_DAYS", "3")),
                      stale_ping_url=os.environ.get("STALE_PING_URL", ""))
    sched.start()
    return create_app(db, cli, runner, installer)


if __name__ == "__main__":
    uvicorn.run(build(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
