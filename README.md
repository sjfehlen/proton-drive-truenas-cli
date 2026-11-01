# proton-drive-truenas-cli

A small container that runs scheduled one-way backups to Proton Drive using Proton's **official `proton-drive` CLI**, with a web page to log in, manage jobs, watch progress and stop a running job. Built for a TrueNAS box where rclone's unofficial Proton backend and a stored TOTP secret kept breaking silently.

> **Status: v0.** Verified against CLI 0.6.0 with a real account: login, session persistence across app restarts, dry run, real uploads (including a 9.8 GB, 100-file job), resume after an interrupted run, change detection, parallel uploads, and the lock-retry path. Not yet tested: session lifetime over days, a container recreate, throughput on tens of thousands of small files. See [docs/cli-notes.md](docs/cli-notes.md) for measurements.

## What it does

- **Web login.** Runs `proton-drive auth login`, shows the URL it prints, and tracks the session. No TOTP secret is stored.
- **Jobs.** Source folder (read-only mount) → Proton folder, cron schedule, exclude patterns, optional Uptime Kuma push URL.
- **Incremental and resumable.** Keeps a SQLite index of uploaded files (path, size, mtime) and uploads only new or changed ones. A batch is indexed only after it uploads successfully, so an interrupted, stopped or failed run continues where it left off.
- **One job at a time**, with a per-job timeout (default 6 hours). A job that overruns is reported failed. A second job started meanwhile waits in a queue and shows as *(queued)*.
- **Small batches.** Uploads go in batches capped by file count and size, so progress is visible and a failure costs at most one batch.
- **Parallel uploads, adjustable live.** The **Parallel uploads** setting on the page (1–8, default 2) controls how many batches upload at once. It is saved, applies immediately without a restart, and a saved value overrides `UPLOAD_WORKERS`. Folders are created one at a time first, so parallel uploads never race on mkdir.
- **Stop job.** Each running or queued job has a **Stop** button. It kills that job's running CLI uploads, skips the rest, and marks the run *stopped*. Files from finished batches are kept and the next run continues. A stop sends **no** Uptime Kuma alert, because it was deliberate. Stopping a queued job cancels it before it starts.
- **Stall watchdog.** A CLI that moves no data (disk or network) for `STALL_SECONDS` is killed and its batch retried once.
- **Lock retries.** Overlapping CLI processes can fail with `SQLite … database is locked` at startup; those calls are retried automatically (see [docs/cli-notes.md](docs/cli-notes.md)).
- **Live progress.** While a job runs the page shows batches done, uploads in flight, bytes uploaded, elapsed time and an estimate. The page never waits on the CLI.
- **Fails loudly.** Pings Uptime Kuma `up` on success and `down` with a short reason on failure — including "session expired, log in" before any work is attempted. An optional staleness ping fires if no job has succeeded in `STALE_DAYS`.
- **Logs to stdout**, so a Docker log collector (Promtail/Loki) picks them up. Every failure is logged, and the first 200 per run are stored and shown on the run's page.
- **Dry run** per job: shows what would upload without touching Proton.

Not in v0: remote deletes (copy only), restore UI, multiple accounts, running several jobs at once.

## Run it

1. Download the Linux `proton-drive` CLI from Proton ([support page](https://proton.me/support/drive-cli)), host it where you control it, and note its SHA-256. Developed against CLI 0.6.0; pin whichever version you test.
2. Copy [compose.example.yaml](compose.example.yaml), set `PROTON_CLI_URL`, `PROTON_CLI_SHA256`, `UI_PASSWORD`, and your host paths.
3. `docker compose up -d --build`, open the page, click **Log in to Proton**, open the URL it shows, finish signing in.
4. Add a job and run it with **Dry run** first.

The UI refuses to start without `UI_PASSWORD` (set `ALLOW_NO_AUTH=1` for local testing only). Do not expose it to the internet without SSO in front.

The page uses relative links, so it also works behind a path-prefix proxy such as code-server's `/proxy/<port>/` (use the trailing slash).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `UI_USER` / `UI_PASSWORD` | `admin` / required | HTTP Basic credentials for the UI |
| `DATA_DIR` | `/data` | State DB and Proton session. The session file is a credential: keep it mode 700, off any shared backup |
| `PORT` | `8080` | Listen port inside the container |
| `STALE_DAYS`, `STALE_PING_URL` | `3`, unset | Optional "no recent success" alert |
| `FILE_CONFLICT_STRATEGY`, `FOLDER_CONFLICT_STRATEGY` | `replace`, `merge` | Passed to `filesystem upload` (values valid in CLI 0.6.0: merge, keep-both, replace, skip) |
| `PROTON_CLI_BIN` | `proton-drive` | CLI path |
| `BATCH_FILES`, `BATCH_MB` | `25`, `500` | Max files and megabytes per CLI upload call. A failure or stall costs at most one batch. Larger batches (50 files / 2000 MB) were used in testing |
| `UPLOAD_WORKERS` | `2` | **Starting value** for parallel uploads (1–8). Once changed on the page, the saved value wins. Measured on CLI 0.6.0: two uploads raised throughput about 1.4x on a ~40–45 Mbps line; more mostly adds retries |
| `STALL_SECONDS` | `600` | An upload that moves no data for this long is killed and the batch retried once. Measured pauses on CLI 0.6.0 reach about 4 minutes, so don't set this low |

## Develop

```bash
uv run pytest
```

Tests use `tests/fake_cli.py`, a stand-in for `proton-drive` that can simulate a dead session, upload failures, silent hangs, slow uploads and the SQLite lock error.

## Security notes

- The Proton session file in `DATA_DIR/session` grants access to the whole Proton account. Treat it like a password.
- Kuma push URLs contain tokens. They are stored in the DB, never rendered in the UI, and never logged.
- Sources are mounted read-only; the container runs as a non-root user.
- The UI is protected by HTTP Basic auth only. Keep it on the LAN or behind SSO.

## Logs

Everything goes to stdout, one event per line, so a Docker log collector picks it up. Every line for a run carries `job=<name> run=<id>`.

| Line | Level | Meaning |
|---|---|---|
| `... start dry_run=... source=... dest=...` | INFO | A run began |
| `... scan done: N scanned, N unchanged, N to upload in N folders` | INFO | Local scan finished |
| `... plan: N files, X GB in N batches (...), N parallel uploads` | INFO | The upload plan |
| `... batch 3/12 done: N files, X MB in Ns (X MB/s); total ...` | INFO | One line per finished batch, with its speed |
| `... batch N/M stalled; retrying (1/1)` | WARNING | The watchdog killed a stalled upload |
| `cli_failed cmd='filesystem upload' exit=1 out='...'` | WARNING | Any CLI call that failed, with its output. Exit 125 = stalled, 124 = timed out, 130 = stopped by you |
| `cli_busy cmd='...' retry N/4 (CLI cache locked by another CLI process)` | WARNING | A call hit `database is locked` and is being retried |
| `... error place='upload /my-files/X' exit 1: ...; batch 3/12 of 50 files, 2.0 GB: a.jpg, b.jpg, c.jpg (+47 more)` | ERROR | One per failed batch or folder, naming the files |
| `stop requested for job id=N` | WARNING | You pressed Stop |
| `... status=ok\|failed\|stopped ... N uploaded, N failed` | INFO / ERROR / WARNING | Run result. **Failed runs are ERROR**, so a Loki alert can match `status=failed`. A stopped run is WARNING |
| `kuma ping failed (<job>): <ExceptionType>` | WARNING | The success/failure ping could not be delivered |

Secrets are never logged: not the Kuma push URL, the login URL or the session. The health-check endpoint is left out of the access log.
