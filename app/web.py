from __future__ import annotations

import html
import json
import os
import secrets
import sqlite3
import threading
import time

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .cli import LoginFlow, ProtonCli
from .db import Db
from .runner import Runner, human

STYLE = """<style>body{font:15px system-ui;margin:2rem auto;max-width:960px;padding:0 1rem}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ccc;padding:.4rem;text-align:left;vertical-align:top}
.ok{color:#1a7f37}.failed{color:#cf222e}.running{color:#9a6700}code{word-break:break-all}
form.inline{display:inline}input{padding:.3rem;margin:.1rem}</style>"""


def esc(s) -> str:
    return html.escape(str(s))


def create_app(db: Db, cli: ProtonCli, runner: Runner) -> FastAPI:
    app = FastAPI(title="proton-drive-truenas-cli")
    basic = HTTPBasic(auto_error=False)
    login = LoginFlow(cli)
    password = os.environ.get("UI_PASSWORD", "")
    user = os.environ.get("UI_USER", "admin")
    if not password and os.environ.get("ALLOW_NO_AUTH") != "1":
        raise RuntimeError("Set UI_PASSWORD (or ALLOW_NO_AUTH=1 for local testing only)")

    def auth(c: HTTPBasicCredentials | None = Depends(basic)):
        if os.environ.get("ALLOW_NO_AUTH") == "1" and not password:
            return
        ok = c is not None and secrets.compare_digest(c.username, user) and secrets.compare_digest(c.password, password)
        if not ok:
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    def bg(fn, *a):
        threading.Thread(target=fn, args=a, daemon=True).start()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def index():
        s = cli.session_status_cached()
        if s is None:
            state = '<b class="running">unknown</b> (a job is using the Proton CLI; checked again when it finishes)'
        else:
            state = '<b class="ok">logged in</b>' if s.ok else f'<b class="failed">not logged in</b> (exit {s.code})'
        if login.running:
            link = f'<a href="{esc(login.url)}" target="_blank" rel="noopener noreferrer">{esc(login.url)}</a>' if login.url else "waiting for URL..."
            lg = f"<p>Login in progress: open {link}, finish signing in, then reload. <form class='inline' method=post action=login/cancel><button>Cancel</button></form></p>"
        else:
            lg = ""
            if login.finished_ok is False:
                lg = f"<p class=failed>Last login attempt failed: {esc(' | '.join(login.lines[-3:]))}</p>"
        pr = runner.progress
        running = ""
        if pr:
            now = time.time()
            rate = pr["done_bytes"] / max(now - pr["started"], 1)
            left = max(pr["bytes"] - pr["done_bytes"], 0)
            eta = f", about {int(left / rate / 60)} min left" if rate > 0 and pr["done_bytes"] else ""
            running = (f"<p class=running><b>Running: {esc(pr['job'])}</b> (run <a href=runs/{pr['run']}>{pr['run']}</a>) &mdash; "
                       f"batch {pr['batch']}/{pr['batches']} "
                       f"({pr.get('batch_files', 0)} files, {human(pr.get('batch_bytes', 0))}, "
                       f"{int(now - pr['batch_started'])}s in); "
                       f"{pr['uploaded']}/{pr['files']} files, {human(pr['done_bytes'])} of {human(pr['bytes'])} uploaded"
                       f"{', ' + str(pr['failed']) + ' failed' if pr['failed'] else ''}; "
                       f"{int(now - pr['started'])}s elapsed{eta}. Reload to refresh.</p>")
        rows = ""
        for j in db.jobs():
            r = db.last_run(j["id"])
            last = (f'<span class="{esc(r["status"])}">{esc(r["status"])}</span> '
                    f'{time.strftime("%m-%d %H:%M", time.localtime(r["started"]))}<br>{esc(r["summary"])}') if r else "never"
            busy = " (running)" if runner.busy_job == j["id"] else ""
            rows += (f"<tr><td><b>{esc(j['name'])}</b>{busy}<br><code>{esc(j['source'])}</code> &rarr; <code>{esc(j['dest'])}</code></td>"
                     f"<td><code>{esc(j['cron'])}</code><br>{'enabled' if j['enabled'] else 'disabled'}</td><td>{last}</td><td>"
                     f"<form class=inline method=post action=jobs/{j['id']}/run><button>Run</button></form> "
                     f"<form class=inline method=post action=jobs/{j['id']}/dryrun><button>Dry run</button></form> "
                     f"<form class=inline method=post action=jobs/{j['id']}/toggle><button>{'Disable' if j['enabled'] else 'Enable'}</button></form> "
                     f"<form class=inline method=post action=jobs/{j['id']}/delete onsubmit=\"return confirm('Delete job?')\"><button>Delete</button></form></td></tr>")
        hist = "".join(f"<tr><td><a href=runs/{r['id']}>{esc(r['name'])}</a>{' (dry)' if r['dry_run'] else ''}</td>"
                       f"<td class='{esc(r['status'])}'>{esc(r['status'])}</td>"
                       f"<td>{time.strftime('%m-%d %H:%M', time.localtime(r['started']))}</td><td>{esc(r['summary'])}</td></tr>"
                       for r in db.recent_runs())
        return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Proton backups</title>{STYLE}
<h1>Proton Drive backups</h1><p>Session: {state}
<form class=inline method=post action=login><button>Log in to Proton</button></form></p>{lg}{running}
<h2>Jobs</h2><table><tr><th>Job</th><th>Schedule</th><th>Last run</th><th></th></tr>{rows}</table>
<h3>Add job</h3><form method=post action=jobs>
<input name=name placeholder=name required> <input name=source placeholder=/source/path required>
<input name=dest placeholder=/my-files/Backups required> <input name=cron placeholder="0 4 * * *" required><br>
<input name=excludes size=60 placeholder="excludes, comma separated, e.g. Data-Storage/thumbs/**"><br>
<input name=kuma_url size=60 placeholder="Uptime Kuma push URL (secret)"> <button>Add</button></form>
<h2>Recent runs</h2><table><tr><th>Job</th><th>Status</th><th>Started</th><th>Summary</th></tr>{hist}</table>"""

    @app.get("/runs/{run_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def run_detail(run_id: int):
        r = db.run(run_id)
        if r is None:
            raise HTTPException(404, "no such run")
        errs = db.run_errors(run_id)
        rows = "".join(f"<tr><td>{time.strftime('%H:%M:%S', time.localtime(e['ts']))}</td><td>{esc(e['place'])}</td>"
                       f"<td>{esc(e['message'])}</td></tr>" for e in errs) or "<tr><td colspan=3>no errors recorded</td></tr>"
        return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Run {run_id}</title>{STYLE}
<p><a href="../">&larr; back</a></p><h1>Run {run_id}: {esc(r['name'])}</h1>
<p class="{esc(r['status'])}"><b>{esc(r['status'])}</b>{' (dry run)' if r['dry_run'] else ''} &middot;
{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['started']))}</p><p>{esc(r['summary'])}</p>
<h2>Errors ({len(errs)})</h2><table><tr><th>Time</th><th>Where</th><th>Message</th></tr>{rows}</table>"""

    @app.post("/login", dependencies=[Depends(auth)])
    def start_login():
        cli.invalidate_status()
        login.start()
        return RedirectResponse("./", 303)

    @app.post("/login/cancel", dependencies=[Depends(auth)])
    def cancel_login():
        login.cancel()
        return RedirectResponse("../", 303)

    @app.post("/jobs", dependencies=[Depends(auth)])
    def add_job(name: str = Form(), source: str = Form(), dest: str = Form(), cron: str = Form(),
                excludes: str = Form(""), kuma_url: str = Form("")):
        from croniter import croniter
        if not croniter.is_valid(cron):
            raise HTTPException(400, "invalid cron expression")
        if not os.path.isdir(source):
            raise HTTPException(400, "source is not a directory inside the container")
        if not dest.startswith("/my-files"):
            raise HTTPException(400, "dest must start with /my-files")
        ex = [e.strip() for e in excludes.split(",") if e.strip()]
        try:
            db.add_job(name, source, dest, cron, ex, kuma_url)
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"a job named {name!r} already exists")
        return RedirectResponse("./", 303)

    @app.post("/jobs/{job_id}/run", dependencies=[Depends(auth)])
    def run(job_id: int):
        bg(runner.run_job, job_id, False)
        return RedirectResponse("../../", 303)

    @app.post("/jobs/{job_id}/dryrun", dependencies=[Depends(auth)])
    def dryrun(job_id: int):
        bg(runner.run_job, job_id, True)
        return RedirectResponse("../../", 303)

    @app.post("/jobs/{job_id}/toggle", dependencies=[Depends(auth)])
    def toggle(job_id: int):
        j = db.job(job_id)
        if j:
            db.set_enabled(job_id, not j["enabled"])
        return RedirectResponse("../../", 303)

    @app.post("/jobs/{job_id}/delete", dependencies=[Depends(auth)])
    def delete(job_id: int):
        db.delete_job(job_id)
        return RedirectResponse("../../", 303)

    return app
