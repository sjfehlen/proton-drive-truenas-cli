#!/usr/bin/env python3
"""Stand-in for `proton-drive`. Logs every call to $FAKE_LOG; behaviour set by env."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")

flaky = os.environ.get("FAKE_FLAKY_ONCE")
if flaky and args[:2] == ["filesystem", "list"] and not os.path.exists(flaky):
    open(flaky, "w").close()
    print("lock busy")
    sys.exit(1)
busy_file = os.environ.get("FAKE_BUSY_FILE")
if busy_file and args[:2] in (["filesystem", "create-folder"], ["filesystem", "upload"]):
    n = int(open(busy_file).read() or 0) if os.path.exists(busy_file) else 0
    if n < int(os.environ.get("FAKE_BUSY_N", "1")):
        open(busy_file, "w").write(str(n + 1))
        print("SQLiteError: database is locked\n errno: 5,\n code: \"SQLITE_BUSY\"")
        sys.exit(1)
if os.environ.get("FAKE_DEAD") == "1":
    print("You need to login first")
    sys.exit(1)
if args[:2] == ["filesystem", "info"]:
    sys.exit(0 if args[2] in os.environ.get("FAKE_EXISTING", "/my-files").split(",") else 1)
if args[:2] == ["filesystem", "upload"]:
    import time
    hang_once = os.environ.get("FAKE_HANG_ONCE")
    if os.environ.get("FAKE_HANG") == "1" or (hang_once and not os.path.exists(hang_once)):
        if hang_once:
            open(hang_once, "w").close()
        time.sleep(600)  # a silent hang
    if os.environ.get("FAKE_SLOW"):
        time.sleep(float(os.environ["FAKE_SLOW"]))
if args[:2] == ["filesystem", "upload"] and os.environ.get("FAKE_THUMB_FAIL") == "1" and "--skip-thumbnails" not in args:
    print("Transfer summary:\n  Uploaded: 1 items\n  Failed: 1 items\n  - x.jpg: ValidationError: Failed to generate thumbnails (use --skip-thumbnails to upload without thumbnails)")
    sys.exit(1)
if args[:2] == ["filesystem", "upload"] and os.environ.get("FAKE_UPLOAD_FAIL") == "1":
    print("boom")
    sys.exit(1)
if args[:2] == ["auth", "login"]:
    print("Open https://account.proton.me/auth/example in your browser")
    sys.stdout.flush()
    sys.exit(0)
sys.exit(0)
