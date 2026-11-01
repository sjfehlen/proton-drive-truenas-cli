"""Adapter around the official `proton-drive` CLI.

Every CLI invocation lives here so a change in the CLI only touches this file.
Checked against proton-drive CLI 0.6.0; see docs/cli-notes.md.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass

URL_RE = re.compile(r"https?://\S+")


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

    def reset_cache(self) -> None:
        self.known_folders.clear()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PROTON_DRIVE_CREDENTIALS_STORE"] = "unsafe_file"
        # The CLI keeps auth-session.json under XDG_DATA_HOME/proton-drive-cli.
        env["XDG_DATA_HOME"] = self.session_dir
        return env

    def _run(self, args: list[str], timeout: float) -> CliResult:
        try:
            p = subprocess.run([self.binary, *args], capture_output=True, text=True,
                               timeout=timeout, env=self._env())
        except subprocess.TimeoutExpired:
            return CliResult(False, 124, "timed out")
        except FileNotFoundError:
            return CliResult(False, 127, f"CLI not found: {self.binary}")
        return CliResult(p.returncode == 0, p.returncode, (p.stdout + p.stderr).strip())

    def session_ok(self) -> CliResult:
        """A cheap authenticated call. A dead session exits 1 with the reason on stdout."""
        return self._run(["filesystem", "list", "/my-files"], 60)

    def folder_exists(self, path: str) -> bool:
        return self._run(["filesystem", "info", path], 60).ok

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
