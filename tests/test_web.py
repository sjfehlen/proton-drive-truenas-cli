import time
import pytest
from fastapi.testclient import TestClient

from app.web import create_app
from tests.test_runner import env  # noqa: F401  (fixture)


def make(env, monkeypatch, password="pw"):
    db, cli, runner, src, log = env
    monkeypatch.setenv("UI_PASSWORD", password)
    return TestClient(create_app(db, cli, runner)), db, src


def test_requires_auth(env, monkeypatch):
    c, _, _ = make(env, monkeypatch)
    assert c.get("/").status_code == 401
    assert c.get("/", auth=("admin", "wrong")).status_code == 401
    assert c.get("/healthz").status_code == 200


def test_refuses_to_start_without_password(env, monkeypatch):
    db, cli, runner, src, log = env
    monkeypatch.delenv("UI_PASSWORD", raising=False)
    monkeypatch.delenv("ALLOW_NO_AUTH", raising=False)
    with pytest.raises(RuntimeError):
        create_app(db, cli, runner)


def test_add_job_validates_and_page_escapes(env, monkeypatch):
    c, db, src = make(env, monkeypatch)
    a = ("admin", "pw")
    bad = c.post("/jobs", auth=a, data={"name": "n", "source": str(src), "dest": "/elsewhere", "cron": "0 4 * * *"})
    assert bad.status_code == 400
    ok = c.post("/jobs", auth=a, follow_redirects=False, data={
        "name": "<script>x</script>", "source": str(src), "dest": "/my-files/B", "cron": "0 4 * * *",
        "excludes": "a/**, b", "kuma_url": "http://k/api/push/SECRET"})
    assert ok.status_code == 303
    page = c.get("/", auth=a).text
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
    assert "SECRET" not in page  # the push URL is never rendered


def test_links_and_redirects_are_relative(env, monkeypatch):
    """Must work behind a path prefix such as code-server's /proxy/8765/."""
    c, db, src = make(env, monkeypatch)
    a = ("admin", "pw")
    page = c.get("/", auth=a).text
    assert 'action="/' not in page and "action=/" not in page
    for path, expect in [("/login", "./"), ("/login/cancel", "../")]:
        assert c.post(path, auth=a, follow_redirects=False).headers["location"] == expect
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *")
    for act in ("toggle", "delete"):
        assert c.post(f"/jobs/{jid}/{act}", auth=a, follow_redirects=False).headers["location"] == "../../"


def test_duplicate_job_name_is_a_409_not_a_500(env, monkeypatch):
    c, db, src = make(env, monkeypatch)
    a = ("admin", "pw")
    form = {"name": "dup", "source": str(src), "dest": "/my-files/B", "cron": "0 4 * * *"}
    assert c.post("/jobs", auth=a, data=form, follow_redirects=False).status_code == 303
    assert c.post("/jobs", auth=a, data=form, follow_redirects=False).status_code == 409


def test_run_detail_page_shows_errors_escaped(env, monkeypatch):
    c, db, src = make(env, monkeypatch)
    a = ("admin", "pw")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *")
    rid = db.start_run(jid, False)
    db.add_error(rid, "upload /my-files/B", "exit 1: <b>bad</b>")
    db.finish_run(rid, "failed", "1 failed")
    page = c.get(f"/runs/{rid}", auth=a).text
    assert "&lt;b&gt;bad&lt;/b&gt;" in page and "<b>bad</b>" not in page
    assert c.get("/runs/9999", auth=a).status_code == 404
    assert f"runs/{rid}" in c.get("/", auth=a).text


def test_page_does_not_block_while_cli_is_busy(env, monkeypatch):
    import time as _t
    c, db, src = make(env, monkeypatch)
    cli = env[1]
    assert cli._meta_lock.acquire(timeout=1)  # simulate a short CLI call holding the lock
    try:
        t0 = _t.time()
        r = c.get("/", auth=("admin", "pw"))
        assert r.status_code == 200 and _t.time() - t0 < 5
        assert "unknown" in r.text
    finally:
        cli._meta_lock.release()


def test_page_shows_live_progress(env, monkeypatch):
    c, db, src = make(env, monkeypatch)
    runner = env[2]
    runner.progress = {"job": "big", "run": 7, "files": 100, "bytes": 10 * 1024**3, "batches": 20, "batch": 3,
                       "active": 2, "workers": 2, "uploaded": 10, "failed": 0,
                       "done_bytes": 1024**3, "started": time.time() - 120, "batch_started": time.time() - 30}
    page = c.get("/", auth=("admin", "pw")).text
    assert "Running: big" in page and "3/20 batches done, 2 uploading" in page and "10/100 files" in page
    runner.progress = None
    assert "Running:" not in c.get("/", auth=("admin", "pw")).text


def test_workers_setting_saves_applies_and_validates(env, monkeypatch):
    c, db, src = make(env, monkeypatch)
    cli = env[1]
    a = ("admin", "pw")
    assert 'name=workers' in c.get("/", auth=a).text
    r = c.post("/settings", auth=a, data={"workers": "5"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "./"
    assert cli.upload_workers == 5 and db.get_setting("upload_workers") == "5"
    assert 'value=5' in c.get("/", auth=a).text
    for bad in ("0", "9", "abc", ""):
        assert c.post("/settings", auth=a, data={"workers": bad}).status_code in (400, 422)
    assert cli.upload_workers == 5  # unchanged by bad input


def test_saved_workers_setting_is_loaded_at_startup(tmp_path, monkeypatch):
    from app.db import Db
    import app.main as m
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UI_PASSWORD", "pw")
    monkeypatch.setenv("UPLOAD_WORKERS", "2")
    Db(str(tmp_path / "state.db")).set_setting("upload_workers", "4")
    monkeypatch.setattr("app.main.Scheduler.start", lambda self: None)
    m.build()
    # build() created its own cli; re-read through the DB the same way it does
    assert Db(str(tmp_path / "state.db")).get_setting("upload_workers") == "4"


def test_stop_button_only_while_running_and_posts_stop(env, monkeypatch):
    c, db, src = make(env, monkeypatch)
    runner = env[2]
    a = ("admin", "pw")
    jid = db.add_job("t", str(src), "/my-files/B", "0 4 * * *")
    assert f"jobs/{jid}/stop" not in c.get("/", auth=a).text
    runner.busy_job = jid
    try:
        page = c.get("/", auth=a).text
        assert f"jobs/{jid}/stop" in page and "(running)" in page
        calls = []
        runner.request_stop = lambda i: calls.append(i) or "stopping"
        r = c.post(f"/jobs/{jid}/stop", auth=a, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "../../" and calls == [jid]
    finally:
        runner.busy_job = None
    assert c.post(f"/jobs/{jid}/stop", auth=("admin", "bad"), follow_redirects=False).status_code == 401
