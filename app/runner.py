"""Runs one backup job: session check, walk, diff against the index, upload, report."""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import PurePosixPath

from .cli import ProtonCli, one_line
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

    def run_job(self, job_id: int, dry_run: bool = False) -> str:
        job = self.db.job(job_id)
        if job is None:
            return "missing"
        with self._lock:
            self.busy_job = job_id
            try:
                return self._run(job, dry_run)
            finally:
                self.busy_job = None

    MAX_STORED_ERRORS = 200

    def _error(self, job, run_id: int, place: str, message: str) -> None:
        """Log every failure; store the first MAX_STORED_ERRORS per run for the web page."""
        log.error("job=%s run=%d error place=%r %s", job["name"], run_id, place, message)
        self._stored = getattr(self, "_stored", {})
        n = self._stored.get(run_id, 0)
        if n < self.MAX_STORED_ERRORS:
            self._stored[run_id] = n + 1
            self.db.add_error(run_id, place, message)
        elif n == self.MAX_STORED_ERRORS:
            self._stored[run_id] = n + 1
            self.db.add_error(run_id, "limit", "further errors not stored; see the container log")

    def _finish(self, job, run_id, status, summary, dry_run):
        self.db.finish_run(run_id, status, summary)
        log.log(logging.ERROR if status != "ok" else logging.INFO,
                "job=%s run=%d status=%s dry_run=%s %s", job["name"], run_id, status, dry_run, summary)
        if not dry_run:
            ping(job["kuma_url"], status == "ok", f"{job['name']}: {summary}", job["name"])
        return status

    def _run(self, job, dry_run: bool) -> str:
        t0 = time.time()
        deadline = t0 + job["timeout_s"]
        run_id = self.db.start_run(job["id"], dry_run)
        self.cli.reset_cache()
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

        uploaded = failed = 0
        done_bytes = 0
        errors: list[str] = []
        plan = []  # (remote_dir, [batches]) so the total batch count is known up front
        for rel_root, items in todo.items():
            remote_dir = str(PurePosixPath(job["dest"]) / rel_root) if rel_root else job["dest"]
            plan.append((remote_dir, items, make_batches(items)))
        total_batches = sum(len(b) for _, _, b in plan)
        total_bytes = sum(it[2] for items in todo.values() for it in items)
        batch_no = 0
        self.progress = {"job": job["name"], "run": run_id, "files": pending, "bytes": total_bytes,
                         "batches": total_batches, "batch": 0, "uploaded": 0, "failed": 0,
                         "done_bytes": 0, "started": t0, "batch_started": t0}
        log.info("job=%s run=%d plan: %d files, %s in %d batches (max %d files / %s each)",
                 job["name"], run_id, pending, human(total_bytes), total_batches, BATCH_FILES, human(BATCH_BYTES))
        try:
            for remote_dir, items, batches in plan:
                r = self.cli.ensure_folder(remote_dir)
                if not r.ok:
                    failed += len(items)
                    batch_no += len(batches)
                    errors.append(f"mkdir {remote_dir}: {r.out[:120]}")
                    self._error(job, run_id, f"mkdir {remote_dir}", f"exit {r.code}: {one_line(r.out, 500)}; {len(items)} files skipped")
                    continue
                for chunk in batches:
                    batch_no += 1
                    if time.time() > deadline:
                        return self._finish(job, run_id, "failed",
                                            f"timeout after {int(time.time()-t0)}s; {uploaded} uploaded, {pending-uploaded} left",
                                            dry_run)
                    size = sum(c[2] for c in chunk)
                    bt = time.time()
                    self.progress.update(batch=batch_no, batch_started=bt, batch_files=len(chunk), batch_bytes=size)
                    r = self.cli.upload([c[0] for c in chunk], remote_dir, max(30.0, deadline - time.time()))
                    tries = 0
                    while not r.ok and r.code == 125 and tries < STALL_RETRIES:
                        tries += 1
                        log.warning("job=%s run=%d batch %d/%d stalled; retrying (%d/%d)",
                                    job["name"], run_id, batch_no, total_batches, tries, STALL_RETRIES)
                        r = self.cli.upload([c[0] for c in chunk], remote_dir, max(30.0, deadline - time.time()))
                    secs = max(time.time() - bt, 0.001)
                    if r.ok:
                        self.db.record_files(job["id"], [(c[1], c[2], c[3]) for c in chunk])
                        uploaded += len(chunk)
                        done_bytes += size
                        log.info("job=%s run=%d batch %d/%d done: %d files, %s in %ds (%s/s); total %d/%d files, %s/%s",
                                 job["name"], run_id, batch_no, total_batches, len(chunk), human(size), int(secs),
                                 human(size / secs), uploaded, pending, human(done_bytes), human(total_bytes))
                    else:
                        failed += len(chunk)
                        errors.append(f"upload {remote_dir}: {r.out[:120]}")
                        names = ", ".join(c[1] for c in chunk[:3]) + (f" (+{len(chunk)-3} more)" if len(chunk) > 3 else "")
                        self._error(job, run_id, f"upload {remote_dir}",
                                    f"exit {r.code}: {one_line(r.out, 500)}; batch {batch_no}/{total_batches} of {len(chunk)} files, {human(size)}: {names}")
                    self.progress.update(uploaded=uploaded, failed=failed, done_bytes=done_bytes)
        finally:
            self.progress = None

        dur = int(time.time() - t0)
        summary = f"{scanned} scanned, {skipped} unchanged, {uploaded} uploaded, {failed} failed, {dur}s"
        if errors:
            summary += f"; {len(errors)} error(s), first: {errors[0]} (see run {run_id} for all)"
        return self._finish(job, run_id, "failed" if failed else "ok", summary, dry_run)
