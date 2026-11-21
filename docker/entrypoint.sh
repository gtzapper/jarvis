#!/bin/sh
# Starts Jarvis. If an in-app update changed requirements.txt since this image was built,
# install the new packages into /config/.pydeps first (Python's user site, which takes priority).
set -u
REQ=/app/requirements.txt
MARK="$PYTHONUSERBASE/requirements.sha"

if [ -f "$REQ" ]; then
    now=$(sha256sum "$REQ" | cut -d' ' -f1)
    if [ "$now" != "$(cat /opt/requirements.sha)" ] && [ "$now" != "$(cat "$MARK" 2>/dev/null)" ]; then
        echo "requirements.txt changed since this image was built: installing new packages..."
        mkdir -p "$PYTHONUSERBASE"
        if pip install --no-cache-dir --user --quiet -r "$REQ"; then
            echo "$now" > "$MARK"
        else
            echo "WARNING: installing new packages failed. Starting anyway; rebuild with: docker compose up -d --build"
        fi
    fi
fi

exec python -m jarvis web --host 0.0.0.0 --port 8765
