# proton-drive CLI notes

What the code assumes about the official CLI, and how sure we are. All calls live in
[app/cli.py](../app/cli.py). **Nothing has been run against the real CLI yet** — tests use
`tests/fake_cli.py`.

| Assumption | Source | Status |
|---|---|---|
| `proton-drive auth login` prints a URL to open in a browser | Proton's CLI support page | Documented |
| `proton-drive filesystem list /my-files`, `filesystem upload <local...> <remote dir>` | Proton's CLI support page | Documented |
| `--json` flag on commands | Proton's CLI support page | Documented |
| `PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file` keeps the session in `~/.local/share/proton-drive-cli/auth-session.json` | Third-party project README | Unverified |
| We redirect that location with `XDG_DATA_HOME=$DATA_DIR/session` | Our guess | **Unverified** — if the CLI ignores it, the session lands in `$HOME` and is lost on container recreate |
| `--file-conflict-strategy` and `--folder-conflict-strategy` are mandatory, otherwise the CLI prompts | Third-party project README | Unverified |
| Values `create-new-revision` (file, CLI >= 0.8.0) and `merge` (folder) | Third-party project README | **Unverified** — override with `FILE_CONFLICT_STRATEGY` / `FOLDER_CONFLICT_STRATEGY` |
| A dead session and an unreachable API both exit 1 | Third-party project README | Unverified |
| `filesystem info <path>` exits non-zero when the path is missing | Third-party project README | Unverified |
| `filesystem create-folder <path>` exists and fails if the folder exists | Third-party project README | Unverified syntax |

## First things to check with the real CLI

1. Log in through the web UI and confirm the session file is written under `$DATA_DIR/session`.
2. Restart the container and confirm the session survives.
3. Run a dry run, then a real run of a small folder. Check conflict-strategy values with `proton-drive filesystem upload --help`.
4. Upload the same folder twice and confirm no duplicates or revision spam.
5. Kill the container mid-run, restart, and confirm the next run resumes (the index only records batches that finished).
6. Compare speed against rclone on the Immich set, and check whether the CLI can see the files rclone already uploaded.
