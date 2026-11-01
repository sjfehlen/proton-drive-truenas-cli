"""Adapter around the official `proton-drive` CLI.

Every CLI invocation lives here so a change in the CLI only touches this file.
Checked against proton-drive CLI 0.6.0; see docs/cli-notes.md.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass

URL_RE = re.compile(r"https?://\S+")
log = logging.getLogger("cli")


def one_line(text: str, limit: int) -> str:
    t = " | ".join(x.strip() for x in text.splitlines() if x.strip())
    return t if len(t) <= limit else t[:limit] + "..."


@dataclass
class CliResult:
    ok: bool
    code: int
    out: str


class ProtonCli:
    def __init__(self, binary: str | None = None, session_dir: str | None = None,
                 file_strategy: str | None = None, folder_strategy: str | None = None):
        self.binary = binary or os.environ.get("PROTON_CLI_BIN", "proton-drive")
        self.session_dir = session_dir or os.environ.get("SESSION_DIR", "/data/session")
        # Verified against CLI 0.6.0: values are merge, keep-both, replace, skip.
        # Newer CLIs (>= 0.8.0) reportedly add create-new-revision for files.
        self.file_strategy = file_strategy or os.environ.get("FILE_CONFLICT_STRATEGY", "replace")
        self.folder_strategy = folder_strategy or os.environ.get("FOLDER_CONFLICT_STRATEGY", "merge")
        # Remote folders already confirmed this run. Each CLI call costs 2-4 s of network round trip,
        # so don't re-probe shared parents. Cleared at the start of every run.
        self.known_folders: set[str] = set()
        # The CLI keeps lock files (events.lock) in its state dir, so overlapping invocations can
        # fail spuriously. Run one CLI process at a time.
        self._cli_lock = threading.Lock()
        self._status: tuple[float, CliResult] | None = None

    def reset_cache(self) -> None:
        self.known_folders.clear()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PROTON_DRIVE_CREDENTIALS_STORE"] = "unsafe_file"
        # The CLI keeps auth-session.json under XDG_DATA_HOME/proton-drive-cli.
        env["XDG_DATA_HOME"] = self.session_dir
        return env

    def _run(self, args: list[str], timeout: float, quiet: bool = False) -> CliResult:
        try:
            with self._cli_lock:
                p = subprocess.run([self.binary, *args], capture_output=True, text=True,
                                   timeout=timeout, env=self._env())
        except subprocess.TimeoutExpired:
            log.warning("cli_failed cmd=%r exit=124 out='timed out after %ss'", " ".join(args[:2]), int(timeout))
            return CliResult(False, 124, "timed out")
        except FileNotFoundError:
            log.error("cli_failed cmd=%r exit=127 out='CLI not found: %s'", " ".join(args[:2]), self.binary)
            return CliResult(False, 127, f"CLI not found: {self.binary}")
        res = CliResult(p.returncode == 0, p.returncode, (p.stdout + p.stderr).strip())
        if not res.ok and not quiet:
            log.warning("cli_failed cmd=%r exit=%d out=%r", " ".join(args[:2]), res.code, one_line(res.out, 400))
        return res

    def session_ok(self) -> CliResult:
        """A cheap authenticated call. A dead session exits 1 with "You need to login first".

        Any other failure (lock contention, network blip) is retried once before it is believed.
        """
        r = self._run(["filesystem", "list", "/my-files"], 60)
        if not r.ok and "login" not in r.out.lower():
            time.sleep(3)
            r = self._run(["filesystem", "list", "/my-files"], 60)
        return r

    @property
    def busy(self) -> bool:
        """True while a CLI process is running (e.g. a long upload)."""
        return self._cli_lock.locked()

    def session_status_cached(self, max_age: float = 30.0) -> CliResult | None:
        """For the web page. Never waits: the page must stay responsive during a long upload.

        Returns the cached result if fresh, None if a CLI call is in progress and nothing is cached.
        """
        now = time.time()
        if self._status and now - self._status[0] < max_age:
            return self._status[1]
        if self.busy:
            return self._status[1] if self._status else None
        r = self.session_ok()
        self._status = (now, r)
        return r

    def invalidate_status(self) -> None:
        self._status = None

    def folder_exists(self, path: str) -> bool:
        # A missing folder is an expected answer here, so a non-zero exit is not logged as a failure.
        return self._run(["filesystem", "info", path], 60, quiet=True).ok

    def create_folder(self, parent: str, name: str) -> CliResult:
        # CLI 0.6.0 syntax: create-folder parentPath name
        return self._run(["filesystem", "create-folder", parent, name], 60)

    def ensure_folder(self, path: str) -> CliResult:
        """Create each missing component of a remote path."""
        parts = [p for p in path.strip("/").split("/") if p]
        cur = f"/{parts[0]}" if parts else ""  # `/my-files` always exists
        for part in parts[1:]:
            nxt = f"{cur}/{part}"
            if nxt not in self.known_folders:
                if not self.folder_exists(nxt):
                    r = self.create_folder(cur, part)
                    if not r.ok:
                        return r
                self.known_folders.add(nxt)
            cur = nxt
        return CliResult(True, 0, "")

    def upload(self, local_files: list[str], remote_dir: str, timeout: float) -> CliResult:
        # Strategies are mandatory: without them the CLI prompts and a service hangs.
        return self._run(
            ["filesystem", "upload",
             "--file-conflict-strategy", self.file_strategy,
             "--folder-conflict-strategy", self.folder_strategy,
             *local_files, remote_dir],
            timeout,
        )


class LoginFlow:
    """Runs `proton-drive auth login`, captures the URL it prints, tracks completion."""

    def __init__(self, cli: ProtonCli):
        self.cli = cli
        self.url: str | None = None
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def finished_ok(self) -> bool | None:
        if self.proc is None or self.proc.poll() is None:
            return None
        return self.proc.returncode == 0

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self.url, self.lines = None, []
            self.proc = subprocess.Popen(
                [self.cli.binary, "auth", "login"], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, env=self.cli._env(),
            )
            threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.lines.append(line.rstrip())
            m = URL_RE.search(line)
            if m and not self.url:
                self.url = m.group(0)

    def cancel(self) -> None:
        if self.running and self.proc:
            self.proc.terminate()
