#!/usr/bin/env python3
"""Stand-in for `proton-drive`. Logs every call to $FAKE_LOG; behaviour set by env."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")

if os.environ.get("FAKE_DEAD") == "1":
    print("Not logged in")
    sys.exit(1)
if args[:2] == ["filesystem", "info"]:
    sys.exit(0 if args[2] in os.environ.get("FAKE_EXISTING", "/my-files").split(",") else 1)
if args[:2] == ["filesystem", "upload"] and os.environ.get("FAKE_UPLOAD_FAIL") == "1":
    print("boom")
    sys.exit(1)
if args[:2] == ["auth", "login"]:
    print("Open https://account.proton.me/auth/example in your browser")
    sys.stdout.flush()
    sys.exit(0)
sys.exit(0)
