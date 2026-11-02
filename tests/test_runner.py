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


# ---- batching, stall watchdog, progress ----

def _item(size, name="f"):
    return (f"/x/{name}", name, size, 0.0)


def test_make_batches_caps_files_and_bytes():
    from app.runner import make_batches
    assert [len(b) for b in make_batches([_item(1)] * 60, max_files=25, max_bytes=10**9)] == [25, 25, 10]
    assert [len(b) for b in make_batches([_item(400)] * 5, max_files=25, max_bytes=1000)] == [2, 2, 1]
    # a file larger than the cap goes alone
    assert [len(b) for b in make_batches([_item(1), _item(5000), _item(1)], max_files=25, max_bytes=1000)] == [1, 1, 1]
    assert make_batches([]) == []


def test_runner_splits_uploads_into_batches(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setattr("app.runner.BATCH_FILES", 25)
    for i in range(60):
        (src / f"bulk{i}.txt").write_text("x")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])
    assert runner.run_job(jid) == "ok"
    sizes = sorted(len([a for a in u if a.startswith("/")]) - 1 for u in uploads(log))
    assert max(sizes) <= 25 and len(uploads(log)) >= 3


def _fast_watchdog(cli, stall=1.0):
    cli.stall_s, cli.poll_s = stall, 0.2
    cli.activity_fn = lambda pid: 0  # never any movement


def test_watchdog_kills_a_silent_hang(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_HANG", "1")
    _fast_watchdog(cli)
    t0 = time.time()
    r = cli.upload([str(src / "top.txt")], "/my-files/B", timeout=60)
    assert not r.ok and r.code == 125 and "stalled" in r.out
    assert time.time() - t0 < 15


def test_watchdog_leaves_an_active_upload_alone(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_SLOW", "2.5")
    cli.stall_s, cli.poll_s = 1.0, 0.2
    n = [0]

    def moving(pid):
        n[0] += 10**6
        return n[0]  # always moving
    cli.activity_fn = moving
    assert cli.upload([str(src / "top.txt")], "/my-files/B", timeout=60).ok


def test_watchdog_falls_back_when_proc_is_unavailable(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_SLOW", "1")

    def no_proc(pid):
        raise OSError("no /proc")
    cli.stall_s, cli.poll_s, cli.activity_fn = 0.1, 0.2, no_proc
    assert cli.upload([str(src / "top.txt")], "/my-files/B", timeout=60).ok  # not killed


def test_stalled_batch_is_retried_once_and_succeeds(env, monkeypatch, tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO)
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_HANG_ONCE", str(tmp_path / "hung"))
    _fast_watchdog(cli)
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])
    assert runner.run_job(jid) == "ok"
    assert len(uploads(log)) == 2  # hung attempt + retry
    assert "stalled; retrying" in caplog.text and "exit=125" in caplog.text
    assert runner.progress is None  # cleared when the run ends


def test_persistent_stall_fails_the_batch_with_a_clear_error(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_HANG", "1")
    _fast_watchdog(cli, stall=0.6)
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])
    assert runner.run_job(jid) == "failed"
    errs = db.run_errors(db.last_run(jid)["id"])
    assert errs and "stalled" in errs[0]["message"] and "exit 125" in errs[0]["message"]
    assert db.indexed(jid) == {}


def test_uploads_run_in_parallel_up_to_the_worker_limit(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setattr("app.runner.BATCH_FILES", 1)
    monkeypatch.setenv("FAKE_SLOW", "2")
    for i in range(3):
        (src / f"p{i}.txt").write_text("x")
    cli.set_upload_workers(2)
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])  # 4 files -> 4 batches
    t0 = time.time()
    assert runner.run_job(jid) == "ok"
    took = time.time() - t0
    assert len(uploads(log)) == 4
    assert took < 7.5, took  # serial would be >= 8 s of sleeping alone


def test_one_worker_stays_serial(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setattr("app.runner.BATCH_FILES", 1)
    monkeypatch.setenv("FAKE_SLOW", "1")
    (src / "extra.txt").write_text("x")
    cli.set_upload_workers(1)
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])  # 2 files -> 2 batches
    t0 = time.time()
    assert runner.run_job(jid) == "ok"
    assert time.time() - t0 >= 2.0


def test_parallel_run_indexes_every_file_exactly_once(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setattr("app.runner.BATCH_FILES", 1)
    for i in range(6):
        (src / f"q{i}.txt").write_text("x")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**"])
    assert runner.run_job(jid) == "ok"
    idx = db.indexed(jid)
    assert len(idx) == 8 and {"top.txt", "a/one.txt", "q0.txt", "q5.txt"} <= set(idx)
    assert runner.run_job(jid) == "ok" and len(uploads(log)) == 8  # second run uploads nothing


# ---- "database is locked" from overlapping CLI processes ----

def test_sqlite_busy_on_create_folder_and_upload_is_retried(env, monkeypatch, tmp_path, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    db, cli, runner, src, log = env
    monkeypatch.setattr("app.cli.time.sleep", lambda s: None)
    monkeypatch.setenv("FAKE_BUSY_FILE", str(tmp_path / "busy"))
    monkeypatch.setenv("FAKE_BUSY_N", "3")  # first 3 create-folder/upload calls hit the lock
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])
    assert runner.run_job(jid) == "ok"
    assert "cli_busy" in caplog.text and db.indexed(jid) == {"top.txt": db.indexed(jid)["top.txt"]}
    assert db.run_errors(db.last_run(jid)["id"]) == []


def test_persistent_busy_gives_up_with_a_clear_error(env, monkeypatch, tmp_path):
    db, cli, runner, src, log = env
    monkeypatch.setattr("app.cli.time.sleep", lambda s: None)
    monkeypatch.setenv("FAKE_BUSY_FILE", str(tmp_path / "busy"))
    monkeypatch.setenv("FAKE_BUSY_N", "999")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])
    assert runner.run_job(jid) == "failed"
    errs = db.run_errors(db.last_run(jid)["id"])
    assert errs and "database is locked" in errs[0]["message"]
    assert db.indexed(jid) == {}


def test_resizable_slots_wake_waiters_when_raised():
    import threading as th
    from app.cli import ResizableSlots
    slots = ResizableSlots(1)
    inside, release = [], th.Event()

    def worker(i):
        with slots:
            inside.append(i)
            release.wait(5)
    ts = [th.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in ts:
        t.start()
    time.sleep(0.3)
    assert len(inside) == 1  # limit 1
    slots.set_limit(3)
    time.sleep(0.3)
    assert len(inside) == 3  # raised live, the waiters got in
    release.set()
    for t in ts:
        t.join(5)


def test_set_upload_workers_clamps(env):
    db, cli, runner, src, log = env
    assert cli.set_upload_workers(0) == 1 and cli.upload_workers == 1
    assert cli.set_upload_workers(99) == 8 and cli.upload_workers == 8


# ---- Stop job ----

def _start_in_thread(runner, jid):
    import threading as th
    t = th.Thread(target=runner.run_job, args=(jid,), daemon=True)
    t.start()
    return t


def _wait_for(cond, secs=15):
    t0 = time.time()
    while time.time() - t0 < secs:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_stop_kills_running_uploads_and_marks_the_run_stopped(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_SLOW", "60")  # uploads would otherwise run for a minute
    monkeypatch.setattr("app.runner.BATCH_FILES", 1)
    (src / "second.txt").write_text("x")
    cli.set_upload_workers(2)
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])
    th_ = _start_in_thread(runner, jid)
    assert _wait_for(lambda: runner.progress and runner.progress["active"] >= 1)
    t0 = time.time()
    assert runner.request_stop(jid) == "stopping"
    th_.join(20)
    assert not th_.is_alive() and time.time() - t0 < 15  # did not wait out the 60 s uploads
    last = db.last_run(jid)
    assert last["status"] == "stopped" and "stopped by user" in last["summary"]
    assert db.indexed(jid) == {}                # killed batches are not recorded
    assert db.run_errors(last["id"]) == []      # a stop is not an error
    assert runner.progress is None and runner.busy_job is None


def test_stop_sends_no_kuma_alert(env, monkeypatch):
    import threading as th
    from http.server import BaseHTTPRequestHandler, HTTPServer
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
    th.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("FAKE_SLOW", "60")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"],
                     kuma_url=f"http://127.0.0.1:{srv.server_port}/api/push/T")
    t = _start_in_thread(runner, jid)
    assert _wait_for(lambda: runner.progress and runner.progress["active"] >= 1)
    runner.request_stop(jid)
    t.join(20)
    srv.shutdown()
    assert db.last_run(jid)["status"] == "stopped" and hits == []


def test_stop_when_nothing_is_running(env):
    db, cli, runner, src, log = env
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *")
    assert runner.request_stop(jid) == "not running"
    # a later run is not affected by an earlier stop request
    assert runner.run_job(jid) == "ok"


def test_stopping_a_queued_job_cancels_it_before_it_starts(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_SLOW", "60")
    a = db.add_job("a", str(src), "/my-files/A", "0 4 * * *", ["skip/**", "a/**"])
    b = db.add_job("b", str(src), "/my-files/B", "0 5 * * *", ["skip/**", "a/**"])
    ta = _start_in_thread(runner, a)
    assert _wait_for(lambda: runner.progress and runner.progress["active"] >= 1)
    tb = _start_in_thread(runner, b)
    assert _wait_for(lambda: b in runner.waiting)
    assert runner.request_stop(b) == "cancelled"
    runner.request_stop(a)
    ta.join(20)
    tb.join(20)
    assert not tb.is_alive()
    assert db.last_run(b) is None  # b never started a run


# ---- glob characters in file names (CLI 0.6.0 expands local paths as globs) ----

def test_glob_escape_only_touches_the_six_glob_characters():
    from app.cli import glob_escape as g
    assert g("AlbumArt_{B50}_Small.jpg") == "AlbumArt_\\{B50\\}_Small.jpg"
    assert g("IMG [1].jpg") == "IMG \\[1\\].jpg"
    assert g("a*b?.txt") == "a\\*b\\?.txt"
    for untouched in ("plain.txt", "a(b).txt", "hey!.txt", "back\\slash.txt", "sp ace.txt", "q'uote.txt", "$d~.txt"):
        assert g(untouched) == untouched  # escaping these breaks the match on the real CLI


def test_upload_passes_escaped_paths_to_the_cli(env):
    db, cli, runner, src, log = env
    (src / "AlbumArt_{B50}.jpg").write_text("x")
    (src / "IMG [1].jpg").write_text("x")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])
    assert runner.run_job(jid) == "ok"
    args = [a for u in uploads(log) for a in u]
    assert any(a.endswith("AlbumArt_\\{B50\\}.jpg") for a in args)
    assert any(a.endswith("IMG \\[1\\].jpg") for a in args)
    # the index and error messages still use the real names
    assert "AlbumArt_{B50}.jpg" in db.indexed(jid) and "IMG [1].jpg" in db.indexed(jid)


def test_unthumbnailable_image_is_retried_without_thumbnails(env, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_THUMB_FAIL", "1")
    (src / "broken.jpg").write_text("not an image")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])
    assert runner.run_job(jid) == "ok"
    ups = uploads(log)
    assert len(ups) == 2 and "--skip-thumbnails" not in ups[0] and "--skip-thumbnails" in ups[1]
    assert "cli_thumbnail_fallback" in caplog.text
    assert "broken.jpg" in db.indexed(jid)  # indexed, so it is not retried every night


# ---- version-aware file-name escaping ----

def test_escaping_style_follows_the_cli_version():
    from app.cli import glob_escape as g, mode_for_version as m
    assert [m(v) for v in ("0.6.0", "0.8.9", "0.9.0", "1.0.0", "", "junk")] == \
        ["backslash", "backslash", "bracket-class", "bracket-class", "backslash", "backslash"]
    assert g("IMG [1].jpg", "bracket-class") == "IMG [[]1[]].jpg"
    assert g("a{b}*?(c)!.txt", "bracket-class") == "a{b}*?(c)!.txt"  # untouched on 0.9.x
    assert g("a{b}.txt", "backslash") == "a\\{b\\}.txt"


def test_new_cli_gets_bracket_class_escaping_from_its_version(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_VERSION", "0.9.0")
    monkeypatch.setenv("FAKE_REJECT_BACKSLASH", "1")
    (src / "IMG [1].txt").write_text("x")
    (src / "br{ace}.txt").write_text("x")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])
    assert runner.run_job(jid) == "ok"
    sent = [a for u in uploads(log) for a in u]
    assert any(a.endswith("IMG [[]1[]].txt") for a in sent) and any(a.endswith("br{ace}.txt") for a in sent)
    assert not any("\\" in a for a in sent)


def test_unknown_version_falls_back_to_the_other_style_and_remembers(env, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    db, cli, runner, src, log = env
    monkeypatch.setenv("FAKE_REJECT_BACKSLASH", "1")  # no FAKE_VERSION: the version is unknown -> backslash first
    (src / "br{ace}.txt").write_text("x")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *", ["skip/**", "a/**"])
    assert runner.run_job(jid) == "ok"
    assert "cli_glob_fallback" in caplog.text and cli.glob_mode == "bracket-class"
    before = len(uploads(log))
    (src / "second{x}.txt").write_text("y")
    assert runner.run_job(jid) == "ok"
    assert len(uploads(log)) == before + 1  # remembered: no failed attempt first this time
