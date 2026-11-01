"""Adapter around the official `proton-drive` CLI.

Every CLI invocation lives here so a change in the CLI only touches this file.
Checked against proton-drive CLI 0.6.0; see docs/cli-notes.md.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

URL_RE = re.compile(r"https?://\S+")
log = logging.getLogger("cli")


MIN_ACTIVITY_BYTES = 64 * 1024  # movement below this over the stall window counts as "nothing happening"


def proc_activity(pid: int) -> int:
    """A counter that rises whenever the process reads files or its network namespace moves bytes.

    Linux only. Raises OSError where /proc is unavailable (the watchdog then falls back to the
    plain timeout). Network bytes come from the process's own netns, so inside the container this is
    effectively the CLI's traffic. It is a coarse signal: unrelated traffic in a shared namespace can
    only hide a stall, never fake one.
    """
    total = 0
    with open(f"/proc/{pid}/io") as f:
        for line in f:
            if line.startswith(("rchar:", "wchar:")):
                total += int(line.split()[1])
    with open(f"/proc/{pid}/net/dev") as f:
        for line in list(f)[2:]:
            name, _, rest = line.partition(":")
            if name.strip() == "lo":
                continue
            cols = rest.split()
            total += int(cols[0]) + int(cols[8])  # rx bytes + tx bytes
    return total


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
                 file_strategy: str | None = None, folder_strategy: str | None = None,
                 stall_s: float | None = None, poll_s: float = 5.0, activity_fn=None):
        self.binary = binary or os.environ.get("PROTON_CLI_BIN", "proton-drive")
        self.session_dir = session_dir or os.environ.get("SESSION_DIR", "/data/session")
        # Verified against CLI 0.6.0: values are merge, keep-both, replace, skip.
        # Newer CLIs (>= 0.8.0) reportedly add create-new-revision for files.
        self.file_strategy = file_strategy or os.environ.get("FILE_CONFLICT_STRATEGY", "replace")
        self.folder_strategy = folder_strategy or os.environ.get("FOLDER_CONFLICT_STRATEGY", "merge")
        # Remote folders already confirmed this run. Each CLI call costs 2-4 s of network round trip,
        # so don't re-probe shared parents. Cleared at the start of every run.
        self.known_folders: set[str] = set()
        # An upload is killed if it moves no data for this long. Measured on CLI 0.6.0: the same upload
        # sometimes finishes in 30 s and sometimes goes quiet for up to ~4 minutes and then resumes by
        # itself (Proton side), so the default leaves a wide margin over that.
        self.stall_s = stall_s if stall_s is not None else float(os.environ.get("STALL_SECONDS", "600"))
        self.poll_s = poll_s
        self.activity_fn = activity_fn or proc_activity
        # Short calls (list/info/create-folder) run one at a time: overlapping them once produced a
        # spurious "session dead" result. Uploads may overlap, up to UPLOAD_WORKERS at once. Measured
        # on CLI 0.6.0: two uploads side by side ran cleanly and raised total throughput about 1.4x.
        self.upload_workers = max(1, int(os.environ.get("UPLOAD_WORKERS", "2")))
        self._meta_lock = threading.Lock()
        self._upload_slots = threading.Semaphore(self.upload_workers)
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
            with self._meta_lock:
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
        """True while a short CLI call (list/info/create-folder) is running."""
        return self._meta_lock.locked()

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
        return self._run_watched(
            ["filesystem", "upload",
             "--file-conflict-strategy", self.file_strategy,
             "--folder-conflict-strategy", self.folder_strategy,
             *local_files, remote_dir],
            timeout,
        )

    def _run_watched(self, args: list[str], timeout: float) -> CliResult:
        """Like _run, but kills the process if it makes no progress for `stall_s` (exit code 125).

        The activity signal includes the network namespace's byte counters, which are shared by all
        uploads in the container. With several uploads running, one hung process is therefore only
        noticed once the others go quiet; the job timeout is the backstop.
        """
        cmd = " ".join(args[:2])
        with self._upload_slots, tempfile.TemporaryFile("w+") as out:
            try:
                proc = subprocess.Popen([self.binary, *args], stdout=out, stderr=subprocess.STDOUT,
                                        text=True, env=self._env())
            except FileNotFoundError:
                log.error("cli_failed cmd=%r exit=127 out='CLI not found: %s'", cmd, self.binary)
                return CliResult(False, 127, f"CLI not found: {self.binary}")
            start = last = time.time()
            watch = True
            try:
                base = self.activity_fn(proc.pid)
            except OSError:
                watch, base = False, 0
                log.warning("stall watchdog unavailable (no /proc); relying on the timeout only")
            reason = ""
            code = 0
            while True:
                try:
                    proc.wait(timeout=self.poll_s)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.time()
                if now - start > timeout:
                    reason, code = f"timed out after {int(timeout)}s", 124
                elif watch:
                    try:
                        a = self.activity_fn(proc.pid)
                    except OSError:
                        a = None  # process ended between the poll and the read
                    if a is not None and a - base >= MIN_ACTIVITY_BYTES:
                        base, last = a, now
                    elif now - last > self.stall_s:
                        reason, code = f"stalled: no data moved for {int(now - last)}s", 125
                if reason:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    break
            out.seek(0)
            text = out.read().strip()
            if reason:
                log.warning("cli_failed cmd=%r exit=%d out=%r", cmd, code, reason)
                return CliResult(False, code, f"{reason}; {one_line(text, 200)}" if text else reason)
            res = CliResult(proc.returncode == 0, proc.returncode, text)
            if not res.ok:
                log.warning("cli_failed cmd=%r exit=%d out=%r", cmd, res.code, one_line(res.out, 400))
            return res


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
