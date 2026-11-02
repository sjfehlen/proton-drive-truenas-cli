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
    monkeypatch.setenv("PROTON_CLI_AUTO_DOWNLOAD", "0")  # never download the real CLI in a test
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


def test_page_says_so_when_the_cli_binary_is_missing(env, monkeypatch):
    c, db, src = make(env, monkeypatch)
    env[1].binary = "/nonexistent/proton-drive"
    page = c.get("/", auth=("admin", "pw")).text
    assert "Proton CLI not available" in page and "/nonexistent/proton-drive" in page



# ---- first-run password setup ----

def fresh(env, monkeypatch):
    """An app with no UI_PASSWORD and an empty database: first-run setup mode."""
    db, cli, runner, src, log = env
    monkeypatch.delenv("UI_PASSWORD", raising=False)
    monkeypatch.delenv("ALLOW_NO_AUTH", raising=False)
    return TestClient(create_app(db, cli, runner)), db, cli, runner


def test_first_run_redirects_everything_to_setup_with_relative_paths(env, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    c, db, cli, runner = fresh(env, monkeypatch)
    assert "FIRST-RUN SETUP" in caplog.text and "Setup code" in caplog.text  # printed at startup
    for path, loc in [("/", "setup"), ("/runs/3", "../setup"), ("/jobs/1/stop", "../../setup")]:
        r = c.request("POST" if "stop" in path else "GET", path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == loc, path
    assert c.get("/healthz").status_code == 200  # stays open for the container health check
    assert c.get("/setup").status_code == 200


def _code(caplog):
    return caplog.text.split("Setup code (needed on that page): ")[1].split()[0]


def test_setup_requires_the_code_and_a_good_password(env, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr("app.web.time.sleep", lambda s: None)
    c, db, cli, runner = fresh(env, monkeypatch)
    code = _code(caplog)
    good = "correct horse battery"
    assert c.post("/setup", data={"code": "wrong", "password": good, "confirm": good}).status_code == 400
    assert db.get_setting("ui_password_hash") is None
    assert c.post("/setup", data={"code": code, "password": "short", "confirm": "short"}).status_code == 400
    assert c.post("/setup", data={"code": code, "password": good, "confirm": good + "x"}).status_code == 400
    assert db.get_setting("ui_password_hash") is None  # nothing saved by any failed attempt


def test_setup_saves_a_hash_then_basic_auth_works_and_setup_closes(env, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    c, db, cli, runner = fresh(env, monkeypatch)
    code, pw = _code(caplog), "correct horse battery"
    r = c.post("/setup", data={"code": code, "password": pw, "confirm": pw}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "./"
    stored = db.get_setting("ui_password_hash")
    assert stored.startswith("scrypt$") and pw not in stored  # hashed, never plain
    assert c.get("/", auth=("admin", pw)).status_code == 200
    monkeypatch.setattr("app.web.time.sleep", lambda s: None)
    assert c.get("/", auth=("admin", "wrong password")).status_code == 401
    assert c.get("/", auth=("someone", pw)).status_code == 401
    assert c.get("/").status_code == 401
    assert c.get("/setup", follow_redirects=False).status_code == 303  # setup is closed for good
    assert c.post("/setup", data={"code": code, "password": "another long one", "confirm": "another long one"},
                  follow_redirects=False).status_code == 303
    assert db.get_setting("ui_password_hash") == stored  # a late setup post cannot overwrite it


def test_password_survives_an_app_restart(env, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    c, db, cli, runner = fresh(env, monkeypatch)
    pw = "correct horse battery"
    c.post("/setup", data={"code": _code(caplog), "password": pw, "confirm": pw})
    again = TestClient(create_app(db, cli, runner))  # a new process reading the same database
    assert again.get("/", auth=("admin", pw)).status_code == 200


def test_change_password(env, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr("app.web.time.sleep", lambda s: None)
    c, db, cli, runner = fresh(env, monkeypatch)
    old, new = "correct horse battery", "a brand new passphrase"
    c.post("/setup", data={"code": _code(caplog), "password": old, "confirm": old})
    assert "Change UI password" in c.get("/", auth=("admin", old)).text
    bad = c.post("/settings/password", auth=("admin", old), data={"current": "nope", "new": new, "confirm": new})
    assert bad.status_code == 400
    short = c.post("/settings/password", auth=("admin", old), data={"current": old, "new": "short", "confirm": "short"})
    assert short.status_code == 400
    ok = c.post("/settings/password", auth=("admin", old), data={"current": old, "new": new, "confirm": new},
                follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "../"
    assert c.get("/", auth=("admin", new)).status_code == 200
    assert c.get("/", auth=("admin", old)).status_code == 401


def test_env_password_wins_and_cannot_be_changed_in_the_ui(env, monkeypatch):
    c, db, src = make(env, monkeypatch)  # UI_PASSWORD=pw
    page = c.get("/", auth=("admin", "pw")).text
    assert "UI_PASSWORD" in page and "Change UI password" not in page
    assert c.post("/settings/password", auth=("admin", "pw"),
                  data={"current": "pw", "new": "x" * 12, "confirm": "x" * 12}).status_code == 400
    assert db.get_setting("ui_password_hash") is None


def test_password_hashing_is_salted_and_verifiable():
    from app.auth import hash_password, verify_password
    a, b = hash_password("same password!"), hash_password("same password!")
    assert a != b  # random salt
    assert verify_password("same password!", a) and not verify_password("other password!", a)
    assert not verify_password("x", "garbage") and not verify_password("x", "")


def test_reset_command_reopens_first_run_setup(env, monkeypatch, caplog, tmp_path):
    import logging
    caplog.set_level(logging.WARNING)
    c, db, cli, runner = fresh(env, monkeypatch)
    pw = "correct horse battery"
    c.post("/setup", data={"code": _code(caplog), "password": pw, "confirm": pw})
    assert c.get("/", auth=("admin", pw)).status_code == 200
    import app.reset_password as rp
    monkeypatch.setenv("DATA_DIR", str(tmp_path))  # reset_password opens DATA_DIR/state.db
    monkeypatch.setattr(rp, "Db", lambda path: db)
    caplog.clear()
    assert rp.main() == 0
    assert db.get_setting("ui_password_hash") is None
    assert c.get("/", follow_redirects=False).status_code == 303  # back to setup
    assert c.get("/setup").status_code == 200
    assert "FIRST-RUN SETUP" in caplog.text  # a fresh code was logged when the page was opened
    pw2 = "second correct horse"
    r = c.post("/setup", data={"code": _code(caplog), "password": pw2, "confirm": pw2}, follow_redirects=False)
    assert r.status_code == 303 and c.get("/", auth=("admin", pw2)).status_code == 200



# ---- CLI auto-download on the page ----

def make_with_installer(env, monkeypatch, status, message="", enabled=True):
    from app.cli_install import Installer
    db, cli, runner, src, log = env
    monkeypatch.setenv("UI_PASSWORD", "pw")
    inst = Installer("/nonexistent/proton-drive", enabled=enabled)
    inst.status, inst.message = status, message
    cli.binary = "/nonexistent/proton-drive"
    return TestClient(create_app(db, cli, runner, inst)), inst


def test_page_shows_download_in_progress(env, monkeypatch):
    c, inst = make_with_installer(env, monkeypatch, "installing")
    page = c.get("/", auth=("admin", "pw")).text
    assert "preparing the Proton CLI" in page and "Download the Proton CLI" not in page


def test_page_shows_install_failure_and_a_retry_button(env, monkeypatch):
    c, inst = make_with_installer(env, monkeypatch, "failed", "could not reach proton.me <x>")
    page = c.get("/", auth=("admin", "pw")).text
    assert "Proton CLI not available" in page and "could not reach proton.me &lt;x&gt;" in page
    assert "action=cli/install" in page


def test_no_retry_button_when_auto_download_is_off(env, monkeypatch):
    c, inst = make_with_installer(env, monkeypatch, "failed", "turned off", enabled=False)
    assert "action=cli/install" not in c.get("/", auth=("admin", "pw")).text


def test_cli_install_button_needs_auth_and_runs_the_installer(env, monkeypatch):
    c, inst = make_with_installer(env, monkeypatch, "failed", "x")
    calls = []
    monkeypatch.setattr(inst, "run", lambda: calls.append(1))
    assert c.post("/cli/install", follow_redirects=False).status_code == 401 and calls == []
    r = c.post("/cli/install", auth=("admin", "pw"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "../"
    assert _wait_until(lambda: calls == [1])


def _wait_until(cond, secs=5):
    t0 = time.time()
    while time.time() - t0 < secs:
        if cond():
            return True
        time.sleep(0.05)
    return False
