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
