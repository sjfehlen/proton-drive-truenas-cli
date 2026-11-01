# proton-drive-truenas-cli

A small container that runs scheduled one-way backups to Proton Drive using Proton's **official `proton-drive` CLI**, with a web page to log in and manage jobs. Built for a TrueNAS box where rclone's unofficial Proton backend and a stored TOTP secret kept breaking silently.

> **Status: v0.** Login, session persistence, dry run and a real upload are verified against CLI 0.6.0. Resume after a crash, session lifetime and throughput at scale are not yet tested. See [docs/cli-notes.md](docs/cli-notes.md).

## What it does

- **Web login.** Runs `proton-drive auth login`, shows the URL it prints, and tracks the session. No TOTP secret is stored.
- **Jobs.** Source folder (read-only mount) → Proton folder, cron schedule, exclude patterns, optional Uptime Kuma push URL.
- **Incremental.** Keeps a SQLite index of uploaded files (path, size, mtime) and uploads only new or changed ones. A batch is indexed only after it uploads successfully.
- **One job at a time**, with a per-job timeout (default 6 hours). A job that overruns is reported failed.
- **Parallel uploads.** Up to `UPLOAD_WORKERS` (default 2) batches upload at once; folders are created one at a time first, so there are no mkdir races.
- **Small batches, stall watchdog.** Uploads go in batches capped by file count and size, so progress is visible and a failure is cheap. A CLI that moves no data for `STALL_SECONDS` is killed and the batch retried once. The page shows the running batch, bytes uploaded and time elapsed.
- **Fails loudly.** Pings Uptime Kuma `up` on success and `down` with a short reason on failure — including "session expired, log in" before any work is attempted. An optional staleness ping fires if no job has succeeded in `STALE_DAYS`.
- **Logs to stdout**, one summary line per run, so a Docker log collector (Promtail/Loki) picks it up.
- **Dry run** per job: shows what would upload without touching Proton.

Not in v0: remote deletes (copy only), restore UI, multiple accounts.

## Run it

1. Download the Linux `proton-drive` CLI from Proton ([support page](https://proton.me/support/drive-cli)), host it where you control it, and note its SHA-256. Developed against CLI 0.6.0; pin whichever version you test.
2. Copy [compose.example.yaml](compose.example.yaml), set `PROTON_CLI_URL`, `PROTON_CLI_SHA256`, `UI_PASSWORD`, and your host paths.
3. `docker compose up -d --build`, open the page, click **Log in to Proton**, open the URL it shows, finish signing in.
4. Add a job and run it with **Dry run** first.

The UI refuses to start without `UI_PASSWORD` (set `ALLOW_NO_AUTH=1` for local testing only). Do not expose it to the internet without SSO in front.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `UI_USER` / `UI_PASSWORD` | `admin` / required | HTTP Basic credentials for the UI |
| `DATA_DIR` | `/data` | State DB and Proton session. The session file is a credential: keep it mode 700, off any shared backup |
| `PORT` | `8080` | Listen port inside the container |
| `STALE_DAYS`, `STALE_PING_URL` | `3`, unset | Optional "no recent success" alert |
| `FILE_CONFLICT_STRATEGY`, `FOLDER_CONFLICT_STRATEGY` | `replace`, `merge` | Passed to `filesystem upload` (values valid in CLI 0.6.0: merge, keep-both, replace, skip) |
| `PROTON_CLI_BIN` | `proton-drive` | CLI path |
| `BATCH_FILES`, `BATCH_MB` | `25`, `500` | Max files and megabytes per CLI upload call. A failure or stall costs at most one batch |
| `UPLOAD_WORKERS` | `2` | Uploads run in parallel, this many at once. Measured on CLI 0.6.0: two uploads side by side raised total throughput about 1.4x on a ~40-45 Mbps line; more would mostly add risk. Set `1` to disable |
| `STALL_SECONDS` | `600` | An upload that moves no data (disk or network) for this long is killed and the batch retried once. Measured pauses on CLI 0.6.0 reach about 4 minutes, so don't set this low |

## Develop

```bash
uv run pytest
```

## Security notes

- The Proton session file in `DATA_DIR/session` grants access to the whole Proton account. Treat it like a password.
- Kuma push URLs contain tokens. They are stored in the DB, never rendered in the UI, and never logged.
- Sources are mounted read-only; the container runs as a non-root user.

## Logs

Everything goes to stdout, one event per line, so a Docker log collector picks it up. Every line for a run carries `job=<name> run=<id>`.

| Line | Level | Meaning |
|---|---|---|
| `... start dry_run=... source=... dest=...` | INFO | A run began |
| `... scan done: N scanned, N unchanged, N to upload in N folders` | INFO | Local scan finished |
| `... progress: x/y uploaded, z failed` | INFO | Every 60 s during long uploads |
| `cli_failed cmd='filesystem upload' exit=1 out='...'` | WARNING | Any CLI call that failed, with its output |
| `... error place='upload /my-files/X' exit 1: ...; batch of 50: a.jpg, b.jpg, c.jpg (+47 more)` | ERROR | One per failed batch or folder, naming the files |
| `... status=ok\|failed ... N uploaded, N failed` | INFO / ERROR | Run result. **Failed runs are ERROR**, so a Loki alert can match `status=failed` |
| `kuma ping failed (<job>): <ExceptionType>` | WARNING | The success/failure ping could not be delivered |

The first 200 errors of each run are also stored and shown on the run's page (click the job name under **Recent runs**). Later errors appear only in the container log. Secrets are never logged: not the Kuma push URL, the login URL or the session.
