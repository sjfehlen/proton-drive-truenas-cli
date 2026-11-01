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

from .cli import ProtonCli
from .db import Db

log = logging.getLogger("runner")
BATCH = 50


def excluded(rel: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if pat.endswith("/**"):
            base = pat[:-3].rstrip("/")
            if rel == base or rel.startswith(base + "/"):
                return True
        elif fnmatch.fnmatch(rel, pat):
            return True
    return False


def ping(url: str, ok: bool, msg: str) -> None:
    """Push to Uptime Kuma. The URL carries a token, so never log it."""
    if not url:
        return
    q = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg[:200]})
    sep = "&" if "?" in url else "?"
    try:
        urllib.request.urlopen(f"{url}{sep}{q}", timeout=10).read()
    except Exception as e:  # a failed ping must never fail the job
        log.warning("kuma ping failed: %s", type(e).__name__)


class Runner:
    def __init__(self, db: Db, cli: ProtonCli):
        self.db, self.cli = db, cli
        self._lock = threading.Lock()  # one job at a time, replaces flock
        self.busy_job: int | None = None

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

    def _finish(self, job, run_id, status, summary, dry_run):
        self.db.finish_run(run_id, status, summary)
        log.info("job=%s status=%s dry_run=%s %s", job["name"], status, dry_run, summary)
        if not dry_run:
            ping(job["kuma_url"], status == "ok", f"{job['name']}: {summary}")
        return status

    def _run(self, job, dry_run: bool) -> str:
        t0 = time.time()
        deadline = t0 + job["timeout_s"]
        run_id = self.db.start_run(job["id"], dry_run)
        self.cli.reset_cache()

        s = self.cli.session_ok()
        if not s.ok:
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
        if dry_run:
            return self._finish(job, run_id, "ok",
                                f"dry run: {scanned} scanned, {skipped} unchanged, {pending} would upload", dry_run)

        uploaded = failed = 0
        errors: list[str] = []
        for rel_root, items in todo.items():
            remote_dir = str(PurePosixPath(job["dest"]) / rel_root) if rel_root else job["dest"]
            r = self.cli.ensure_folder(remote_dir)
            if not r.ok:
                failed += len(items)
                errors.append(f"mkdir {remote_dir}: {r.out[:120]}")
                continue
            for i in range(0, len(items), BATCH):
                if time.time() > deadline:
                    return self._finish(job, run_id, "failed",
                                        f"timeout after {int(time.time()-t0)}s; {uploaded} uploaded, {pending-uploaded} left",
                                        dry_run)
                chunk = items[i:i + BATCH]
                r = self.cli.upload([c[0] for c in chunk], remote_dir, max(30.0, deadline - time.time()))
                if r.ok:
                    self.db.record_files(job["id"], [(c[1], c[2], c[3]) for c in chunk])
                    uploaded += len(chunk)
                else:
                    failed += len(chunk)
                    errors.append(f"upload {remote_dir}: {r.out[:120]}")

        dur = int(time.time() - t0)
        summary = f"{scanned} scanned, {skipped} unchanged, {uploaded} uploaded, {failed} failed, {dur}s"
        if errors:
            summary += f"; first error: {errors[0]}"
        return self._finish(job, run_id, "failed" if failed else "ok", summary, dry_run)
