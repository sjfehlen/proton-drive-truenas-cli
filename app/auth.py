"""UI password handling: first-run setup, hashed storage, optional environment override."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading

from .db import Db

log = logging.getLogger("auth")

SETTING_KEY = "ui_password_hash"
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


class Auth:
    """Where the UI password comes from, in priority order:

    1. `UI_PASSWORD` in the environment (managed outside the app; cannot be changed in the UI).
    2. A hash saved in the database, created on first run through the setup page or changed on the page.
    3. Nothing yet: first-run setup. Everything except /healthz and /setup redirects to /setup, which
       needs a one-time setup code printed in the container log, so that only someone who can read the
       log can claim the app.

    `ALLOW_NO_AUTH=1` (local testing only) turns authentication off.
    """

    MIN_LEN = 10

    def __init__(self, db: Db):
        self.db = db
        self.user = os.environ.get("UI_USER", "admin")
        self.env_password = os.environ.get("UI_PASSWORD", "")
        self.no_auth = os.environ.get("ALLOW_NO_AUTH") == "1" and not self.env_password
        self._setup_code: str | None = None
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        if self.no_auth:
            return "open"
        if self.env_password:
            return "env"
        return "db" if self.db.get_setting(SETTING_KEY) else "setup"

    def check(self, username: str, password: str) -> bool:
        mode = self.mode
        if mode == "open":
            return True
        user_ok = secrets.compare_digest(username.encode(), self.user.encode())
        if mode == "env":
            return secrets.compare_digest(password.encode(), self.env_password.encode()) and user_ok
        stored = self.db.get_setting(SETTING_KEY) or ""
        # Always run the hash so timing does not reveal whether the username was right.
        return verify_password(password, stored) and user_ok

    def set_password(self, new: str) -> None:
        self.db.set_setting(SETTING_KEY, hash_password(new))
        with self._lock:
            self._setup_code = None
        log.warning("UI password was set or changed")

    def setup_code(self) -> str:
        """The one-time code for first-run setup. Created on demand and written to the log."""
        with self._lock:
            if self._setup_code is None:
                self._setup_code = secrets.token_urlsafe(9)
                log.warning("FIRST-RUN SETUP: open the web page and create the UI password. "
                            "Setup code (needed on that page): %s", self._setup_code)
            return self._setup_code

    def code_matches(self, given: str) -> bool:
        return secrets.compare_digest(given.strip().encode(), self.setup_code().encode())

    @staticmethod
    def password_problem(password: str, confirm: str) -> str | None:
        if len(password) < Auth.MIN_LEN:
            return f"The password must be at least {Auth.MIN_LEN} characters."
        if password != confirm:
            return "The two passwords do not match."
        return None
