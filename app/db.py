from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, source TEXT NOT NULL,
  dest TEXT NOT NULL, cron TEXT NOT NULL, excludes TEXT NOT NULL DEFAULT '[]',
  kuma_url TEXT NOT NULL DEFAULT '', timeout_s INTEGER NOT NULL DEFAULT 21600,
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, started REAL NOT NULL,
  finished REAL, status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', dry_run INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_errors (
  run_id INTEGER NOT NULL, ts REAL NOT NULL, place TEXT NOT NULL, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_errors_run ON run_errors(run_id);
CREATE TABLE IF NOT EXISTS files (
  job_id INTEGER NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL,
  PRIMARY KEY (job_id, path)
);
"""


class Db:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.tx() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.tx() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))

    def delete_setting(self, key: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM settings WHERE key=?", (key,))

    # jobs
    def add_job(self, name, source, dest, cron, excludes=(), kuma_url="", timeout_s=21600) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO jobs(name,source,dest,cron,excludes,kuma_url,timeout_s) VALUES(?,?,?,?,?,?,?)",
                (name, source, dest, cron, json.dumps(list(excludes)), kuma_url, timeout_s))
            return cur.lastrowid

    def jobs(self) -> list[sqlite3.Row]:
        with self.tx() as c:
            return c.execute("SELECT * FROM jobs ORDER BY name").fetchall()

    def job(self, job_id: int) -> sqlite3.Row | None:
        with self.tx() as c:
            return c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def set_enabled(self, job_id: int, enabled: bool) -> None:
        with self.tx() as c:
            c.execute("UPDATE jobs SET enabled=? WHERE id=?", (int(enabled), job_id))

    def delete_job(self, job_id: int) -> None:
        with self.tx() as c:
            for t in ("files", "runs"):
                c.execute(f"DELETE FROM {t} WHERE job_id=?", (job_id,))
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    # runs
    def start_run(self, job_id: int, dry_run: bool) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO runs(job_id,started,status,dry_run) VALUES(?,?,?,?)",
                             (job_id, time.time(), "running", int(dry_run))).lastrowid

    def finish_run(self, run_id: int, status: str, summary: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET finished=?, status=?, summary=? WHERE id=?",
                      (time.time(), status, summary, run_id))

    def last_run(self, job_id: int) -> sqlite3.Row | None:
        with self.tx() as c:
            return c.execute("SELECT * FROM runs WHERE job_id=? AND dry_run=0 ORDER BY id DESC LIMIT 1",
                             (job_id,)).fetchone()

    def last_success(self) -> float | None:
        with self.tx() as c:
            r = c.execute("SELECT MAX(finished) m FROM runs WHERE status='ok' AND dry_run=0").fetchone()
            return r["m"]

    def recent_runs(self, limit=20) -> list[sqlite3.Row]:
        with self.tx() as c:
            return c.execute("SELECT r.*, j.name FROM runs r JOIN jobs j ON j.id=r.job_id "
                             "ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()

    def mark_stale_runs(self) -> None:
        """A run still 'running' at startup was killed by a restart."""
        with self.tx() as c:
            c.execute("UPDATE runs SET status='failed', summary='interrupted by restart', finished=? "
                      "WHERE status='running'", (time.time(),))

    def add_error(self, run_id: int, place: str, message: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO run_errors(run_id,ts,place,message) VALUES(?,?,?,?)",
                      (run_id, time.time(), place, message))

    def run_errors(self, run_id: int) -> list[sqlite3.Row]:
        with self.tx() as c:
            return c.execute("SELECT * FROM run_errors WHERE run_id=? ORDER BY ts", (run_id,)).fetchall()

    def run(self, run_id: int) -> sqlite3.Row | None:
        with self.tx() as c:
            return c.execute("SELECT r.*, j.name FROM runs r JOIN jobs j ON j.id=r.job_id WHERE r.id=?",
                             (run_id,)).fetchone()

    # uploaded-file index
    def indexed(self, job_id: int) -> dict[str, tuple[int, float]]:
        with self.tx() as c:
            return {r["path"]: (r["size"], r["mtime"])
                    for r in c.execute("SELECT path,size,mtime FROM files WHERE job_id=?", (job_id,))}

    def record_files(self, job_id: int, rows: list[tuple[str, int, float]]) -> None:
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO files(job_id,path,size,mtime) VALUES(?,?,?,?)",
                          [(job_id, p, s, m) for p, s, m in rows])
