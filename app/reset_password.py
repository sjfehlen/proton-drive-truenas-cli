"""Forgot the UI password? Run inside the container:

    docker exec proton-backup python -m app.reset_password

This deletes the saved password. The next visit to the page shows first-run setup again, with a new
setup code in the container log. Jobs, history and the Proton login are not touched. It does nothing
if the password comes from the UI_PASSWORD environment variable (change that variable instead).
"""
from __future__ import annotations

import os
import sys

from .auth import SETTING_KEY
from .db import Db


def main() -> int:
    if os.environ.get("UI_PASSWORD"):
        print("UI_PASSWORD is set in the environment, so the password is managed there. "
              "Change or remove that variable and restart the container.")
        return 1
    db = Db(os.path.join(os.environ.get("DATA_DIR", "/data"), "state.db"))
    if db.get_setting(SETTING_KEY) is None:
        print("No saved password; first-run setup is already open.")
        return 0
    db.delete_setting(SETTING_KEY)
    print("Password removed. Open the web page to create a new one; the setup code appears in the container log.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
