from __future__ import annotations

import html
import json
import os
import secrets
import sqlite3
import threading
import time

import logging

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .auth import Auth
from .cli import MAX_UPLOAD_WORKERS, LoginFlow, ProtonCli
from .cli_install import Installer
from .db import Db
from .runner import Runner, human

STYLE = """<style>body{font:15px system-ui;margin:2rem auto;max-width:960px;padding:0 1rem}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ccc;padding:.4rem;text-align:left;vertical-align:top}
.ok{color:#1a7f37}.stopped{color:#6e7781}.failed{color:#cf222e}.running{color:#9a6700}code{word-break:break-all}
form.inline{display:inline}input{padding:.3rem;margin:.1rem}</style>"""


log = logging.getLogger("web")


def esc(s) -> str:
    return html.escape(str(s))


def create_app(db: Db, cli: ProtonCli, runner: Runner, installer: Installer | None = None) -> FastAPI:
    app = FastAPI(title="proton-drive-truenas-cli")
    basic = HTTPBasic(auto_error=False)
    login = LoginFlow(cli)
    auth_state = Auth(db)
    if auth_state.mode == "setup":
        auth_state.setup_code()  # logs the first-run setup code at startup
    elif auth_state.mode == "open":
        log.warning("ALLOW_NO_AUTH=1: the UI has NO authentication. Local testing only.")

    def auth(request: Request, c: HTTPBasicCredentials | None = Depends(basic)):
        mode = auth_state.mode
        if mode == "open":
            return
        if mode == "setup":
            # Relative redirect, so it also works behind a path-prefix proxy (e.g. code-server /proxy/PORT/).
            ups = request.url.path.strip("/").count("/")
            raise HTTPException(303, headers={"Location": "../" * ups + "setup"})
        if c is None or not auth_state.check(c.username, c.password):
            if c is not None:
                log.warning("failed UI login for user %r from %s", c.username[:40],
                            request.client.host if request.client else "?")
                time.sleep(1)  # slows password guessing
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
        elif installer is not None and installer.status == "installing":
            state = '<b class="running">preparing the Proton CLI</b> (downloading and checking it; reload in a minute)'
        elif s.code == 127 or (installer is not None and installer.status == "failed"):
            why = f" {esc(installer.message)}." if installer is not None and installer.status == "failed" else ""
            retry = (" <form class=inline method=post action=cli/install><button>Download the Proton CLI</button></form>"
                     if installer is not None and installer.enabled else "")
            state = (f'<b class="failed">Proton CLI not available</b> at <code>{esc(cli.binary)}</code>.{why}{retry}')
        else:
            state = '<b class="ok">logged in</b>' if s.ok else f'<b class="failed">not logged in</b> (exit {s.code})'
        if login.running:
            link = f'<a href="{esc(login.url)}" target="_blank" rel="noopener noreferrer">{esc(login.url)}</a>' if login.url else "waiting for URL..."
            lg = f"<p>Login in progress: open {link}, finish signing in, then reload. <form class='inline' method=post action=login/cancel><button>Cancel</button></form></p>"
        else:
            lg = ""
            if login.finished_ok is False:
                lg = f"<p class=failed>Last login attempt failed: {esc(' | '.join(login.lines[-3:]))}</p>"
        if auth_state.mode == "env":
            pw_section = ("<p><small>The UI password is set by the <code>UI_PASSWORD</code> environment variable. "
                          "Remove it to manage the password here.</small></p>")
        elif auth_state.mode == "open":
            pw_section = "<p class=failed><b>Authentication is OFF</b> (ALLOW_NO_AUTH=1). Local testing only.</p>"
        else:
            pw_section = (f"<details><summary>Change UI password</summary><form method=post action=settings/password>"
                          f"<input type=password name=current placeholder='current password' autocomplete=current-password required> "
                          f"<input type=password name=new placeholder='new password (min {Auth.MIN_LEN})' autocomplete=new-password required> "
                          f"<input type=password name=confirm placeholder='repeat new password' autocomplete=new-password required> "
                          f"<button>Change</button></form></details>")
        pr = runner.progress
        running = ""
        if pr:
            now = time.time()
            running = (f"<p class=running><b>Running: {esc(pr['job'])}</b> (run <a href=runs/{pr['run']}>{pr['run']}</a>) &mdash; "
                       f"{pr['batch']}/{pr['batches']} batches done, {pr.get('active', 0)} uploading "
                       f"(up to {cli.upload_workers} at once); "
                       f"{pr['uploaded']}/{pr['files']} files, {human(pr['done_bytes'])} of {human(pr['bytes'])} uploaded"
                       f"{', ' + str(pr['failed']) + ' failed' if pr['failed'] else ''}; "
                       f"{int(now - pr['started'])}s elapsed. Progress counts a batch only when it finishes. Reload to refresh.</p>")
        rows = ""
        for j in db.jobs():
            r = db.last_run(j["id"])
            last = (f'<span class="{esc(r["status"])}">{esc(r["status"])}</span> '
                    f'{time.strftime("%m-%d %H:%M", time.localtime(r["started"]))}<br>{esc(r["summary"])}') if r else "never"
            is_running = runner.busy_job == j["id"]
            is_queued = j["id"] in runner.waiting
            busy = " (running)" if is_running else " (queued)" if is_queued else ""
            stop_btn = (f"<form class=inline method=post action=jobs/{j['id']}/stop "
                        f"onsubmit=\"return confirm('Stop this job? Finished uploads are kept; running ones are cancelled.')\">"
                        f"<button>Stop</button></form> ") if (is_running or is_queued) else ""
            rows += (f"<tr><td><b>{esc(j['name'])}</b>{busy}<br><code>{esc(j['source'])}</code> &rarr; <code>{esc(j['dest'])}</code></td>"
                     f"<td><code>{esc(j['cron'])}</code><br>{'enabled' if j['enabled'] else 'disabled'}</td><td>{last}</td><td>"
                     f"{stop_btn}<form class=inline method=post action=jobs/{j['id']}/run><button>Run</button></form> "
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
<h2>Settings</h2><form method=post action=settings>Parallel uploads
<input type=number name=workers min=1 max={MAX_UPLOAD_WORKERS} value={cli.upload_workers} style="width:4em">
<button>Save</button> <small>1&ndash;{MAX_UPLOAD_WORKERS}. Applies right away: running uploads finish, new ones follow the new limit.
More is not always faster (the internet line and Proton are the limit) and overlapping uploads sometimes retry on
&ldquo;database is locked&rdquo;.</small></form>
{pw_section}
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

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(error: str = ""):
        if auth_state.mode != "setup":
            return RedirectResponse("./", 303)
        auth_state.setup_code()  # make sure a code exists and is in the log (e.g. after a password reset)
        return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Set up</title>{STYLE}
<h1>First-run setup</h1>
<p>Create the password for this page. The user name will be <b>{esc(auth_state.user)}</b>.</p>
<p>Enter the <b>setup code</b> from the container log (look for <code>FIRST-RUN SETUP</code>). It proves you have access to the server,
so nobody else on the network can claim this page first.</p>
{f'<p class=failed><b>{esc(error)}</b></p>' if error else ''}
<form method=post action=setup>
<p><input name=code placeholder="setup code" size=24 autocomplete=off required></p>
<p><input type=password name=password placeholder="new password (min {Auth.MIN_LEN} characters)" size=32 autocomplete=new-password required></p>
<p><input type=password name=confirm placeholder="repeat password" size=32 autocomplete=new-password required></p>
<button>Create password</button></form>"""

    @app.post("/setup", response_class=HTMLResponse)
    def setup_submit(request: Request, code: str = Form(""), password: str = Form(""), confirm: str = Form("")):
        if auth_state.mode != "setup":
            return RedirectResponse("./", 303)
        problem = None
        if not auth_state.code_matches(code):
            log.warning("first-run setup: wrong setup code from %s", request.client.host if request.client else "?")
            time.sleep(1)
            problem = "The setup code is wrong. Copy it from the container log."
        else:
            problem = Auth.password_problem(password, confirm)
        if problem:
            return HTMLResponse(setup_page(problem), status_code=400)
        auth_state.set_password(password)
        return RedirectResponse("./", 303)

    @app.post("/settings/password", dependencies=[Depends(auth)])
    def change_password(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form("")):
        if auth_state.mode != "db":
            raise HTTPException(400, "The password is managed outside the UI (UI_PASSWORD or no auth).")
        if not auth_state.check(auth_state.user, current):
            log.warning("password change refused: wrong current password from %s", request.client.host if request.client else "?")
            time.sleep(1)
            raise HTTPException(400, "The current password is wrong.")
        problem = Auth.password_problem(new, confirm)
        if problem:
            raise HTTPException(400, problem)
        auth_state.set_password(new)
        return RedirectResponse("../", 303)

    @app.post("/settings", dependencies=[Depends(auth)])
    def save_settings(workers: str = Form()):
        try:
            n = int(workers)
        except ValueError:
            raise HTTPException(400, "workers must be a whole number")
        if not 1 <= n <= MAX_UPLOAD_WORKERS:
            raise HTTPException(400, f"workers must be between 1 and {MAX_UPLOAD_WORKERS}")
        db.set_setting("upload_workers", str(cli.set_upload_workers(n)))
        return RedirectResponse("./", 303)

    @app.post("/cli/install", dependencies=[Depends(auth)])
    def cli_install():
        if installer is not None and installer.status != "installing":
            bg(installer.run)
        return RedirectResponse("../", 303)

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

    @app.post("/jobs/{job_id}/stop", dependencies=[Depends(auth)])
    def stop(job_id: int):
        runner.request_stop(job_id)
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
