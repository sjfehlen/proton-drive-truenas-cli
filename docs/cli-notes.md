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
| **Local paths are expanded as globs, and the rules differ by version.** 0.6.0: `{ } [ ] * ?` passed raw fail (`No paths matched`); backslash-escaping exactly those six works, while escaping `( ) ! \` breaks those names. 0.9.0: raw `{ } * ? ( )` work and backslashes BREAK them; only `[` and `]` need protecting, as `[[]` and `[]]`. Remote folder names with glob characters work unescaped and remote files keep their literal names | Verified on 0.6.0 and 0.9.0. `glob_escape(path, mode)` picks the style from the CLI version and falls back to the other style on `No paths matched` (log `cli_glob_fallback`) |
| A batch containing an image the CLI can't thumbnail fails with `Failed to generate thumbnails (use --skip-thumbnails ...)` while the rest of the batch uploads | Verified (0.6.0). The adapter retries that batch once with `--skip-thumbnails` (log `cli_thumbnail_fallback`) |
| **CLI 0.9.0 compatibility** (2026-11-01): `list`, `info`, `create-folder parent name` and `upload` have the same syntax; an existing 0.6.0 session works unchanged; `replace` (file) and `merge` (folder), the app's defaults, are valid. 0.9.0 drops `merge` and `keep-both` for files, adds `create-new-revision` and `rename`, removes the all-purpose `-c` flag, and skips files with identical content on its own | Verified for these commands; a full 100-file run and the thumbnail fallback were not repeated on 0.9.0 |
| **Downloads**: Proton publishes `https://proton.me/download/drive/cli/index.html` (latest version, a URL and a SHA-512 per platform: linux x64/arm64, glibc and musl) and keeps old versions at `/cli/<version>/<platform>/proton-drive`. The official 0.6.0 download is byte-identical to the binary used for all testing | Verified. The app pins 0.6.0 and 0.9.0 checksums in `app/cli_install.py` |
| Strategy flags are mandatory, otherwise the CLI prompts | Documented in `--help`; we always pass them |

## Measured

- Each CLI call costs about 2-4 s of network round trip, regardless of file size. A 10-file batch uploaded in about 6 s.
- A three-file, one-subfolder job took 37 s end to end, mostly folder probing. The runner now caches confirmed folders per run, so shared parents are probed once.

- Sustained upload of large files ran at about 3.5 MiB/s (29 Mbps) over a 10 minute window.
- **Pauses are intermittent.** The same 31 MB file uploaded in about 30 s with steady traffic on one attempt, and on another attempt sat with almost no traffic for over two minutes. In the 100-file run the CLI went quiet for roughly four minutes and then resumed on its own. The stall watchdog default (600 s) is set from this.
- A CLI process reports activity through `/proc/<pid>/io` (bytes read) and its network namespace counters; the watchdog uses both and needs Linux.

- **Two uploads at once work.** A second CLI process alongside a running upload finished cleanly (3 files, 163 MiB in 90 s, no lock collisions) and raised combined throughput from about 2.8 to 3.8 MB/s (1.37x, noisy baseline). The line (Spectrum, roughly 40-45 Mbps up) was close to full, so more than 2 workers is unlikely to help.
- Short calls (`list`, `info`, `create-folder`) are still serialized in the app; they ran fine next to uploads when tried by hand.

- **Overlapping CLI processes can fail with `SQLiteError: database is locked (SQLITE_BUSY)`.** The CLI opens a SQLite cache in its state dir at startup. A `create-folder` (and probably any call) that starts while another process has it busy dies before doing any work. Seen with two parallel uploads: 5 `create-folder` calls failed in one run and skipped 24 files. The adapter now retries these up to 5 times with a 1-3 s random wait (log line `cli_busy`). Likely also the cause of the one-off "session dead" result seen earlier.

- **Killing the CLI mid-upload is safe.** The app was restarted several times with CLI uploads killed in flight (SIGTERM, then SIGKILL). Every resume skipped exactly the files from finished batches and re-uploaded the rest with `replace`, with no duplicate files and nothing added to the Proton trash. The Stop button uses the same SIGTERM.
- **Resume cost:** an interrupted run loses only the batches in flight, so batch size sets the worst case (2 GB at the largest tested size).

## Environment notes

- Gateway check on 2026-11-01: uploads left through the Spectrum WAN, with the gateway sending about 40 Mbps upstream in total while the CLI used roughly 25-35 Mbps of that. The line, not only the CLI, limits speed. Run comparisons against other tools over the same WAN at the same time of day.

## Still to test

1. Recreate the container (not just the process) and confirm the session survives in the mounted volume.
2. Session lifetime: leave it for several days and check that jobs still authenticate.
3. Many small files: a tree with thousands of files and hundreds of folders. Each folder costs a probe plus a create (2-4 s each), so folder-heavy trees may be slow.
4. Compare speed against rclone on the same data and WAN, and check whether the CLI can see files rclone already uploaded.
5. Switching the default to 0.9.0: repeat a large multi-file run, the thumbnail fallback and the lock-retry behaviour on it first. It would add same-content skipping and `create-new-revision`.
6. Whether more than 2 parallel uploads helps. One 5-worker run on a ~40-45 Mbps line was **not faster** than 1-2 workers (about 1.9 MB/s overall over its first 7 minutes) and produced more lock retries. It used many small per-folder batches (3-8 files), so it is inconclusive; repeat with large batches before drawing a conclusion.
