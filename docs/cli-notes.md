# proton-drive CLI notes

What the code assumes about the official CLI. All calls live in [app/cli.py](../app/cli.py).
**Verified against CLI 0.6.0** (`cli-drive@0.6.0`, SDK `js@0.19.2`) on 2026-11-01 with a real login,
a dry run and a real upload.

| Behaviour | Status |
|---|---|
| `auth login` prints a URL; the login page captures it and the session then persists | Verified |
| `PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file` is required headless (default needs libsecret) | Verified |
| The CLI honours `XDG_DATA_HOME`; the session lands in `$DATA_DIR/session/proton-drive-cli/auth-session.json` | Verified |
| A missing or dead session exits 1 with "You need to login first" | Verified |
| `filesystem list /my-files` works as the cheap session check | Verified |
| `filesystem info <path>` exits non-zero for a missing path | Verified |
| `filesystem create-folder <parentPath> <name>` (two arguments) | Verified |
| `filesystem upload -f <strategy> -d <strategy> <local...> <parent>` creates the file; values are `merge`, `keep-both`, `replace`, `skip` | Verified (0.6.0). `create-new-revision` is not available in 0.6.0 |
| `replace` on a changed file overwrites it in place: new content, no duplicate, nothing added to `/trash` (checked 2026-11-01) | Verified (0.6.0) |
| The session survives restarting the app process | Verified (two restarts); a full container recreate is not yet tested |
| Strategy flags are mandatory, otherwise the CLI prompts | Documented in `--help`; we always pass them |

## Measured

- Each CLI call costs about 2-4 s of network round trip, regardless of file size. A 10-file batch uploaded in about 6 s.
- A three-file, one-subfolder job took 37 s end to end, mostly folder probing. The runner now caches confirmed folders per run, so shared parents are probed once.

## Still to test

1. Recreate the container (not just the process) and confirm the session survives in the mounted volume.
2. Kill the process mid-run, restart, and confirm the next run resumes (the index only records batches that finished).
3. ~~Change a file and confirm only that file uploads~~ — done: one file uploaded in 11 s.
4. Compare speed against rclone on the Immich set, and check whether the CLI can see the files rclone already uploaded.
5. Session lifetime: leave it for several days and check that jobs still authenticate.
6. Newer CLI builds (0.8.0+) rename strategies; re-check `--help` before upgrading.
