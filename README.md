# proton-drive-truenas-cli

A small container that runs scheduled one-way backups to Proton Drive using Proton's **official `proton-drive` CLI**, with a web page to log in and manage jobs. Built for a TrueNAS box where rclone's unofficial Proton backend and a stored TOTP secret kept breaking silently.

> **Status: v0, untested against the real CLI.** The scheduler, runner, index, Kuma pings and web UI are covered by tests using a fake CLI. The CLI calls themselves are unverified — see [docs/cli-notes.md](docs/cli-notes.md).

## What it does

- **Web login.** Runs `proton-drive auth login`, shows the URL it prints, and tracks the session. No TOTP secret is stored.
- **Jobs.** Source folder (read-only mount) → Proton folder, cron schedule, exclude patterns, optional Uptime Kuma push URL.
- **Incremental.** Keeps a SQLite index of uploaded files (path, size, mtime) and uploads only new or changed ones. A batch is indexed only after it uploads successfully.
- **One job at a time**, with a per-job timeout (default 6 hours). A job that overruns is reported failed.
- **Fails loudly.** Pings Uptime Kuma `up` on success and `down` with a short reason on failure — including "session expired, log in" before any work is attempted. An optional staleness ping fires if no job has succeeded in `STALE_DAYS`.
- **Logs to stdout**, one summary line per run, so a Docker log collector (Promtail/Loki) picks it up.
- **Dry run** per job: shows what would upload without touching Proton.

Not in v0: remote deletes (copy only), restore UI, multiple accounts.

## Run it

1. Download the Linux `proton-drive` CLI from Proton ([support page](https://proton.me/support/drive-cli)), host it where you control it, and note its SHA-256. Use version 0.8.0 or later.
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
| `FILE_CONFLICT_STRATEGY`, `FOLDER_CONFLICT_STRATEGY` | `create-new-revision`, `merge` | Passed to `filesystem upload` (unverified values) |
| `PROTON_CLI_BIN` | `proton-drive` | CLI path |

## Develop

```bash
uv run pytest
```

## Security notes

- The Proton session file in `DATA_DIR/session` grants access to the whole Proton account. Treat it like a password.
- Kuma push URLs contain tokens. They are stored in the DB, never rendered in the UI, and never logged.
- Sources are mounted read-only; the container runs as a non-root user.
