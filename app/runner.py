"""Runs one backup job: session check, walk, diff against the index, upload, report."""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import PurePosixPath

from .cli import MAX_UPLOAD_WORKERS, ProtonCli, one_line
from .db import Db

log = logging.getLogger("runner")
BATCH_FILES = int(os.environ.get("BATCH_FILES", "25"))
BATCH_BYTES = int(os.environ.get("BATCH_MB", "500")) * 1024 * 1024
STALL_RETRIES = 1


def make_batches(items, max_files: int | None = None, max_bytes: int | None = None):
    """Split (full, rel, size, mtime) tuples into batches capped by file count and total bytes.

    A file bigger than max_bytes gets a batch of its own. Small batches mean progress is visible,
    and a failure or stall costs at most one batch.
    """
    max_files, max_bytes = max_files or BATCH_FILES, max_bytes or BATCH_BYTES
    batches, cur, cur_bytes = [], [], 0
    for it in items:
        if cur and (len(cur) >= max_files or cur_bytes + it[2] > max_bytes):
            batches.append(cur)
            cur, cur_bytes = [], 0
        cur.append(it)
        cur_bytes += it[2]
    if cur:
        batches.append(cur)
    return batches


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def excluded(rel: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if pat.endswith("/**"):
            base = pat[:-3].rstrip("/")
            if rel == base or rel.startswith(base + "/"):
                return True
        elif fnmatch.fnmatch(rel, pat):
            return True
    return False


def ping(url: str, ok: bool, msg: str, name: str = "") -> None:
    """Push to Uptime Kuma. The URL carries a token, so never log it."""
    if not url:
        return
    q = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg[:200]})
    sep = "&" if "?" in url else "?"
    try:
        urllib.request.urlopen(f"{url}{sep}{q}", timeout=10).read()
    except Exception as e:  # a failed ping must never fail the job
        log.warning("kuma ping failed (%s): %s", name or "?", type(e).__name__)


class Runner:
    def __init__(self, db: Db, cli: ProtonCli):
        self.db, self.cli = db, cli
        self._lock = threading.Lock()  # one job at a time, replaces flock
        self.busy_job: int | None = None
        self.progress: dict | None = None  # live view for the web page while a job runs
        self.stop_requested = threading.Event()
        self.waiting: set[int] = set()          # jobs queued behind the running one
        self._cancelled_waiting: set[int] = set()
        self._stored: dict[int, int] = {}
        self._err_lock = threading.Lock()

    def run_job(self, job_id: int, dry_run: bool = False) -> str:
        job = self.db.job(job_id)
        if job is None:
            return "missing"
        self.waiting.add(job_id)
        with self._lock:
            self.waiting.discard(job_id)
            if job_id in self._cancelled_waiting:  # stopped while still queued
                self._cancelled_waiting.discard(job_id)
                log.info("job=%s queued run cancelled before it started", job["name"])
                return "stopped"
            self.busy_job = job_id
            try:
                return self._run(job, dry_run)
            finally:
                self.busy_job = None

    MAX_STORED_ERRORS = 200

    def request_stop(self, job_id: int) -> str:
        """Stop a running job (kill its uploads, skip the rest) or cancel a queued one."""
        if self.busy_job == job_id:
            log.warning("stop requested for job id=%d", job_id)
            self.stop_requested.set()
            self.cli.cancel_uploads()
            return "stopping"
        if job_id in self.waiting:
            self._cancelled_waiting.add(job_id)
            return "cancelled"
        return "not running"

    def _error(self, job, run_id: int, place: str, message: str) -> None:
        """Log every failure; store the first MAX_STORED_ERRORS per run for the web page."""
        log.error("job=%s run=%d error place=%r %s", job["name"], run_id, place, message)
        with self._err_lock:
            n = self._stored.get(run_id, 0)
            self._stored[run_id] = n + 1 if n <= self.MAX_STORED_ERRORS else n
        if n < self.MAX_STORED_ERRORS:
            self.db.add_error(run_id, place, message)
        elif n == self.MAX_STORED_ERRORS:
            self.db.add_error(run_id, "limit", "further errors not stored; see the container log")

    def _finish(self, job, run_id, status, summary, dry_run):
        self.db.finish_run(run_id, status, summary)
        level = {"ok": logging.INFO, "stopped": logging.WARNING}.get(status, logging.ERROR)
        log.log(level, "job=%s run=%d status=%s dry_run=%s %s", job["name"], run_id, status, dry_run, summary)
        if not dry_run and status != "stopped":  # a deliberate stop is not an alert
            ping(job["kuma_url"], status == "ok", f"{job['name']}: {summary}", job["name"])
        return status

    def _run(self, job, dry_run: bool) -> str:
        t0 = time.time()
        deadline = t0 + job["timeout_s"]
        run_id = self.db.start_run(job["id"], dry_run)
        self.cli.reset_cache()
        self.cli.reset_cancel()
        self.stop_requested.clear()
        log.info("job=%s run=%d start dry_run=%s source=%s dest=%s", job["name"], run_id, dry_run,
                 job["source"], job["dest"])

        s = self.cli.session_ok()
        if not s.ok:
            self._error(job, run_id, "session", f"exit {s.code}: {one_line(s.out, 300)}")
            return self._finish(job, run_id, "failed",
                                f"session not usable (exit {s.code}) - log in via the web UI", dry_run)

        excludes = json.loads(job["excludes"])
        indexed = self.db.indexed(job["id"])
        todo: dict[str, list[tuple[str, str, int, float]]] = defaultdict(list)
        scanned = skipped = 0
        src = job["source"]
        for root, dirs, files in os.walk(src):
            rel_root = os.path.relpath(root, src).replace(os.sep, "/")
            rel_root = "" if rel_root == "." else rel_root
            dirs[:] = [d for d in dirs if not excluded(f"{rel_root}/{d}".lstrip("/"), excludes)]
            for f in files:
                rel = f"{rel_root}/{f}".lstrip("/")
                if excluded(rel, excludes):
                    continue
                full = os.path.join(root, f)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                scanned += 1
                if indexed.get(rel) == (st.st_size, st.st_mtime):
                    skipped += 1
                    continue
                todo[rel_root].append((full, rel, st.st_size, st.st_mtime))

        pending = sum(len(v) for v in todo.values())
        log.info("job=%s run=%d scan done: %d scanned, %d unchanged, %d to upload in %d folders (%ds)",
                 job["name"], run_id, scanned, skipped, pending, len(todo), int(time.time() - t0))
        if dry_run:
            return self._finish(job, run_id, "ok",
                                f"dry run: {scanned} scanned, {skipped} unchanged, {pending} would upload", dry_run)

        errors: list[str] = []
        st = {"uploaded": 0, "failed": 0, "done_bytes": 0, "timed_out": 0}
        st_lock = threading.Lock()
        plan = []  # (remote_dir, items, batches) so the total batch count is known up front
        for rel_root, items in todo.items():
            remote_dir = str(PurePosixPath(job["dest"]) / rel_root) if rel_root else job["dest"]
            plan.append((remote_dir, items, make_batches(items)))
        total_batches = sum(len(b) for _, _, b in plan)
        total_bytes = sum(it[2] for items in todo.values() for it in items)
        workers = self.cli.upload_workers
        self.progress = {"job": job["name"], "run": run_id, "files": pending, "bytes": total_bytes,
                         "batches": total_batches, "batch": 0, "active": 0, "workers": workers,
                         "uploaded": 0, "failed": 0, "done_bytes": 0, "started": t0}
        log.info("job=%s run=%d plan: %d files, %s in %d batches (max %d files / %s each), %d parallel uploads",
                 job["name"], run_id, pending, human(total_bytes), total_batches, BATCH_FILES,
                 human(BATCH_BYTES), workers)

        def work(chunk, remote_dir, batch_no):
            if self.stop_requested.is_set():
                return
            if time.time() > deadline:
                with st_lock:
                    st["timed_out"] += len(chunk)
                return
            size = sum(c[2] for c in chunk)
            bt = time.time()
            with st_lock:
                self.progress["active"] += 1
            try:
                r = self.cli.upload([c[0] for c in chunk], remote_dir, max(30.0, deadline - time.time()))
                tries = 0
                while not r.ok and r.code == 125 and tries < STALL_RETRIES:
                    tries += 1
                    log.warning("job=%s run=%d batch %d/%d stalled; retrying (%d/%d)",
                                job["name"], run_id, batch_no, total_batches, tries, STALL_RETRIES)
                    r = self.cli.upload([c[0] for c in chunk], remote_dir, max(30.0, deadline - time.time()))
            finally:
                with st_lock:
                    self.progress["active"] -= 1
            if r.code == 130:  # killed by Stop: not a failure, and not indexed (the next run redoes it)
                return
            secs = max(time.time() - bt, 0.001)
            if r.ok:
                self.db.record_files(job["id"], [(c[1], c[2], c[3]) for c in chunk])
                with st_lock:
                    st["uploaded"] += len(chunk)
                    st["done_bytes"] += size
                    self.progress.update(batch=self.progress["batch"] + 1, uploaded=st["uploaded"],
                                         done_bytes=st["done_bytes"])
                    snap = dict(st)
                log.info("job=%s run=%d batch %d/%d done: %d files, %s in %ds (%s/s); total %d/%d files, %s/%s",
                         job["name"], run_id, batch_no, total_batches, len(chunk), human(size), int(secs),
                         human(size / secs), snap["uploaded"], pending, human(snap["done_bytes"]), human(total_bytes))
            else:
                names = ", ".join(c[1] for c in chunk[:3]) + (f" (+{len(chunk)-3} more)" if len(chunk) > 3 else "")
                errors.append(f"upload {remote_dir}: {r.out[:120]}")
                with st_lock:
                    st["failed"] += len(chunk)
                    self.progress.update(batch=self.progress["batch"] + 1, failed=st["failed"])
                self._error(job, run_id, f"upload {remote_dir}",
                            f"exit {r.code}: {one_line(r.out, 500)}; batch {batch_no}/{total_batches} of {len(chunk)} files, {human(size)}: {names}")

        try:
            batch_no = 0
            # The pool is sized at the cap; the CLI's resizable limiter decides how many upload at once,
            # so the setting can change mid-run.
            with ThreadPoolExecutor(max_workers=MAX_UPLOAD_WORKERS, thread_name_prefix="upload") as pool:
                for remote_dir, items, batches in plan:
                    if self.stop_requested.is_set():
                        break
                    # Folders are created here, one at a time, so parallel uploads never race on mkdir.
                    r = self.cli.ensure_folder(remote_dir)
                    if not r.ok:
                        batch_no += len(batches)
                        errors.append(f"mkdir {remote_dir}: {r.out[:120]}")
                        with st_lock:
                            st["failed"] += len(items)
                            self.progress.update(batch=self.progress["batch"] + len(batches), failed=st["failed"])
                        self._error(job, run_id, f"mkdir {remote_dir}", f"exit {r.code}: {one_line(r.out, 500)}; {len(items)} files skipped")
                        continue
                    for chunk in batches:
                        batch_no += 1
                        pool.submit(work, chunk, remote_dir, batch_no)
        finally:
            self.progress = None
        uploaded, failed = st["uploaded"], st["failed"]
        if self.stop_requested.is_set():
            return self._finish(job, run_id, "stopped",
                                f"stopped by user after {int(time.time()-t0)}s; {uploaded} uploaded, {failed} failed, "
                                f"{pending-uploaded-failed} not done. Uploaded files are kept; the next run continues",
                                dry_run)
        if st["timed_out"]:
            return self._finish(job, run_id, "failed",
                                f"timeout after {int(time.time()-t0)}s; {uploaded} uploaded, {pending-uploaded} left",
                                dry_run)

        dur = int(time.time() - t0)
        summary = f"{scanned} scanned, {skipped} unchanged, {uploaded} uploaded, {failed} failed, {dur}s"
        if errors:
            summary += f"; {len(errors)} error(s), first: {errors[0]} (see run {run_id} for all)"
        return self._finish(job, run_id, "failed" if failed else "ok", summary, dry_run)
