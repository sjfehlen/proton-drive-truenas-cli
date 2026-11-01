import json
import os
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.cli import LoginFlow, ProtonCli
from app.db import Db
from app.runner import Runner, excluded

FAKE = os.path.join(os.path.dirname(__file__), "fake_cli.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    wrapper = tmp_path / "proton-drive"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "skip" / "deep").mkdir(parents=True)
    (src / "a" / "one.txt").write_text("1")
    (src / "top.txt").write_text("t")
    (src / "skip" / "deep" / "x.bin").write_text("x")
    db = Db(str(tmp_path / "s.db"))
    cli = ProtonCli(binary=str(wrapper), session_dir=str(tmp_path / "sess"))
    return db, cli, Runner(db, cli), src, log


def calls(log):
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def uploads(log):
    return [c for c in calls(log) if c[:2] == ["filesystem", "upload"]]


def test_excluded():
    assert excluded("skip/deep/x.bin", ["skip/**"])
    assert excluded("skip", ["skip/**"])
    assert not excluded("skipper/x", ["skip/**"])
    assert excluded("a/b.tmp", ["*.tmp"])


def test_upload_then_skip_unchanged(env):
    db, cli, runner, src, log = env
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**"])
    assert runner.run_job(jid) == "ok"
    ups = uploads(log)
    assert len(ups) == 2  # root dir and a/
    flat = " ".join(" ".join(u) for u in ups)
    mk = [c for c in calls(log) if c[:2] == ["filesystem", "create-folder"]]
    assert ["filesystem", "create-folder", "/my-files", "B"] in mk
    assert ["filesystem", "create-folder", "/my-files/B", "a"] in mk
    assert "x.bin" not in flat and "--file-conflict-strategy" in flat and "--folder-conflict-strategy" in flat
    assert runner.run_job(jid) == "ok"
    assert len(uploads(log)) == 2  # nothing new uploaded the second time
    (src / "top.txt").write_text("changed!")
    runner.run_job(jid)
    assert len(uploads(log)) == 3


def test_dry_run_uploads_nothing(env):
    db, cli, runner, src, log = env
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *")
    runner.run_job(jid, dry_run=True)
    assert uploads(log) == []
    assert db.indexed(jid) == {}


def test_dead_session_fails_fast_and_skips_work(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_DEAD", "1")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *")
    assert runner.run_job(jid) == "failed"
    assert uploads(log) == []
    assert "log in" in db.last_run(jid)["summary"]


def test_upload_failure_is_not_indexed(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_UPLOAD_FAIL", "1")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *")
    assert runner.run_job(jid) == "failed"
    assert db.indexed(jid) == {}


def test_kuma_ping_up_and_down(env, monkeypatch):
    db, cli, runner, src, log = env
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/api/push/TOKEN"
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", kuma_url=url)
    runner.run_job(jid)
    monkeypatch.setenv("FAKE_DEAD", "1")
    runner.run_job(jid)
    srv.shutdown()
    assert "status=up" in hits[0] and "status=down" in hits[1]


def test_login_flow_captures_url(env):
    db, cli, runner, src, log = env
    flow = LoginFlow(cli)
    flow.start()
    for _ in range(50):
        if flow.url:
            break
        time.sleep(0.1)
    assert flow.url == "https://account.proton.me/auth/example"


def test_shared_parent_folders_probed_once_per_run(env):
    db, cli, runner, src, log = env
    (src / "a" / "b").mkdir()
    (src / "a" / "b" / "deep.txt").write_text("d")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**"])
    runner.run_job(jid)
    probes = [c[2] for c in calls(log) if c[:2] == ["filesystem", "info"]]
    assert len(probes) == len(set(probes))  # no path probed twice


def test_transient_session_check_failure_is_retried(env, monkeypatch, tmp_path):
    db, cli, runner, src, log = env
    monkeypatch.setattr("app.cli.time.sleep", lambda s: None)
    monkeypatch.setenv("FAKE_FLAKY_ONCE", str(tmp_path / "flaky"))
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *")
    assert runner.run_job(jid) == "ok"


def test_login_required_is_not_retried(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_DEAD", "1")
    runner.run_job(db.add_job("t", str(src), "/my-files/B", "0 4 * * *"))
    lists = [c for c in calls(log) if c[:2] == ["filesystem", "list"]]
    assert len(lists) == 1


def test_failures_are_logged_stored_and_named(env, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_UPLOAD_FAIL", "1")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**"])
    assert runner.run_job(jid) == "failed"
    run_id = db.last_run(jid)["id"]
    stored = db.run_errors(run_id)
    assert len(stored) == 2  # one per failed batch (root dir and a/)
    assert all("boom" in e["message"] and "exit 1" in e["message"] for e in stored)
    assert any("one.txt" in e["message"] for e in stored)  # names the files in the batch
    text = caplog.text
    assert "cli_failed" in text and "start dry_run" in text and "scan done" in text
    assert "ERROR" in text and f"run={run_id} status=failed" in text
    assert "see run" in db.last_run(jid)["summary"]


def test_session_failure_records_cli_output(env, monkeypatch, caplog):
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_DEAD", "1")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *")
    runner.run_job(jid)
    errs = db.run_errors(db.last_run(jid)["id"])
    assert errs and errs[0]["place"] == "session" and "login" in errs[0]["message"]


def test_error_storage_is_capped(env):
    db, cli, runner, src, log = env
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *")
    job = db.job(jid)
    rid = db.start_run(jid, False)
    for i in range(runner.MAX_STORED_ERRORS + 50):
        runner._error(job, rid, "x", f"e{i}")
    assert len(db.run_errors(rid)) == runner.MAX_STORED_ERRORS + 1  # + the "limit" marker
