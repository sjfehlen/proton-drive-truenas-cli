#!/bin/sh
# Starts as root only to make sure the data folder belongs to the app user, then drops privileges.
#
# Why: if the host folder for /data does not exist yet, Docker creates it owned by root, and an app
# running as a normal user then cannot write to it. Fixing the ownership here means the folder does
# not have to be prepared by hand on the host (the dev box has no shell access to bender).
set -e
PUID="${PUID:-568}"
PGID="${PGID:-568}"
DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    if [ "$(stat -c %u "$DATA_DIR")" != "$PUID" ]; then
        chown -R "$PUID:$PGID" "$DATA_DIR"
    fi
    chmod 700 "$DATA_DIR"   # the Proton session file inside is a credential
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --no-new-privs python -m app.main
fi

exec python -m app.main
