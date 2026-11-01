"""Adapter around the official `proton-drive` CLI.

Every CLI invocation lives here so a change in the CLI only touches this file.
Checked against proton-drive CLI 0.6.0; see docs/cli-notes.md.
"""
from __future__ import annotations

import logging
import os
import random
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

URL_RE = re.compile(r"https?://\S+")
log = logging.getLogger("cli")


# The CLI keeps a SQLite cache in its state dir. A second CLI process starting while another has the
# file busy dies in its constructor with "database is locked" before doing any work, so retrying is
# safe. Seen in practice when create-folder or an upload started next to a running upload.
BUSY_MARKERS = ("database is locked", "SQLITE_BUSY")
BUSY_ATTEMPTS = 5


MAX_UPLOAD_WORKERS = 8


class ResizableSlots:
    """A semaphore whose limit can change while it is in use (raising it wakes waiters at once;
    lowering it lets running uploads finish and applies to the next ones)."""

    def __init__(self, limit: int):
        self._limit, self._used = limit, 0
        self._cond = threading.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    def set_limit(self, n: int) -> None:
        with self._cond:
            self._limit = n
            self._cond.notify_all()

    def __enter__(self):
        with self._cond:
            while self._used >= self._limit:
                self._cond.wait()
            self._used += 1
        return self

    def __exit__(self, *exc) -> None:
        with self._cond:
            self._used -= 1
            self._cond.notify_all()


def is_busy(text: str) -> bool:
    return any(m in text for m in BUSY_MARKERS)


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


_GLOB_CHARS = re.compile(r"([*?\[\]{}])")


def glob_escape(path: str) -> str:
    """Backslash-escape the characters the CLI treats as glob syntax in local path arguments.

    Verified on CLI 0.6.0: it expands local paths as globs, so a file such as `AlbumArt_{GUID}.jpg` or
    `IMG [1].jpg` fails with "No paths matched" when passed raw. Only these six characters need a
    backslash. Escaping others breaks the match: `\\(`, `\\!` and `\\\\` are taken literally, so
    names with parentheses, `!` or a backslash must be passed as they are.
    """
    return _GLOB_CHARS.sub(r"\\\1", path)


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
        self._upload_slots = ResizableSlots(
            max(1, min(MAX_UPLOAD_WORKERS, int(os.environ.get("UPLOAD_WORKERS", "2")))))
        self._meta_lock = threading.Lock()
        self._cancel = threading.Event()  # set by "Stop job": running uploads are killed, queued ones skipped
        self._status: tuple[float, CliResult] | None = None

    @property
    def upload_workers(self) -> int:
        return self._upload_slots.limit

    def set_upload_workers(self, n: int) -> int:
        n = max(1, min(MAX_UPLOAD_WORKERS, int(n)))
        self._upload_slots.set_limit(n)
        return n

    def cancel_uploads(self) -> None:
        self._cancel.set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    def reset_cache(self) -> None:
        self.known_folders.clear()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PROTON_DRIVE_CREDENTIALS_STORE"] = "unsafe_file"
        # The CLI keeps auth-session.json under XDG_DATA_HOME/proton-drive-cli.
        env["XDG_DATA_HOME"] = self.session_dir
        return env

    def _run(self, args: list[str], timeout: float, quiet: bool = False) -> CliResult:
        for attempt in range(1, BUSY_ATTEMPTS + 1):
            res = self._run_once(args, timeout, quiet)
            if res.ok or not is_busy(res.out) or attempt == BUSY_ATTEMPTS:
                return res
            self._busy_wait(args, attempt)
        return res

    def _busy_wait(self, args: list[str], attempt: int) -> None:
        log.warning("cli_busy cmd=%r retry %d/%d (CLI cache locked by another CLI process)",
                    " ".join(args[:2]), attempt, BUSY_ATTEMPTS - 1)
        time.sleep(random.uniform(1.0, 3.0))

    def _run_once(self, args: list[str], timeout: float, quiet: bool = False) -> CliResult:
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
        res = self._upload_with_retries(local_files, remote_dir, timeout, skip_thumbnails=False)
        # CLI 0.6.0 refuses an image it cannot thumbnail ("Failed to generate thumbnails (use
        # --skip-thumbnails ...)"). One corrupt or odd image would fail its batch on every run, so
        # retry the batch without thumbnails: the file is still backed up, just without a preview.
        if not res.ok and "Failed to generate thumbnails" in res.out:
            log.warning("cli_thumbnail_fallback retrying batch of %d files without thumbnails", len(local_files))
            res = self._upload_with_retries(local_files, remote_dir, timeout, skip_thumbnails=True)
        return res

    def _upload_with_retries(self, local_files: list[str], remote_dir: str, timeout: float,
                             skip_thumbnails: bool) -> CliResult:
        # Strategies are mandatory: without them the CLI prompts and a service hangs.
        args = ["filesystem", "upload",
                "--file-conflict-strategy", self.file_strategy,
                "--folder-conflict-strategy", self.folder_strategy]
        if skip_thumbnails:
            args.append("--skip-thumbnails")
        args += [*[glob_escape(f) for f in local_files], remote_dir]
        for attempt in range(1, BUSY_ATTEMPTS + 1):
            res = self._run_watched(args, timeout)
            if res.ok or not is_busy(res.out) or attempt == BUSY_ATTEMPTS:
                return res
            self._busy_wait(args, attempt)
        return res

    def _run_watched(self, args: list[str], timeout: float) -> CliResult:
        """Like _run, but kills the process if it makes no progress for `stall_s` (exit code 125).

        The activity signal includes the network namespace's byte counters, which are shared by all
        uploads in the container. With several uploads running, one hung process is therefore only
        noticed once the others go quiet; the job timeout is the backstop.
        """
        cmd = " ".join(args[:2])
        with self._upload_slots, tempfile.TemporaryFile("w+") as out:
            if self._cancel.is_set():  # stopped while waiting for a free upload slot
                return CliResult(False, 130, "stopped by user")
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
            tick, last_poll = min(self.poll_s, 1.0), start
            while True:
                try:
                    proc.wait(timeout=tick)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.time()
                if self._cancel.is_set():
                    reason, code = "stopped by user", 130
                elif now - start > timeout:
                    reason, code = f"timed out after {int(timeout)}s", 124
                elif watch and now - last_poll >= self.poll_s:
                    last_poll = now
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
                (log.info if code == 130 else log.warning)("cli_failed cmd=%r exit=%d out=%r", cmd, code, reason)
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
