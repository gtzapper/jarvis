#!/usr/bin/env bash
# Update Jarvis from the command line (the same as the button in Setup > Jarvis version).
#
#   ./update.sh             check GitHub, show what's new, ask, update and restart
#   ./update.sh --yes       the same without asking
#   ./update.sh --rollback  go back to the version before the last update
#   ./update.sh --rebuild   rebuild the Docker image (needed when an update changes the Dockerfile)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
running() { docker ps --format '{{.Names}}' | grep -qx jarvis; }

case "${1:-}" in
    --rebuild)
        say "Rebuilding the image and restarting"
        docker compose up -d --build
        exit 0 ;;
    --rollback)
        running || { echo "Jarvis isn't running. Start it first: docker compose up -d"; exit 1; }
        docker exec jarvis python -m jarvis update --rollback
        docker restart jarvis >/dev/null
        say "Restarted on the previous version"
        exit 0 ;;
esac

if ! running; then
    # Container is down (maybe a bad update): update the checkout directly, then rebuild and start.
    say "Jarvis isn't running, so updating the code directly"
    owner="$(stat -c '%u:%g' .)"
    git -c safe.directory="$PWD" pull --ff-only
    chown -R "$owner" .
    docker compose up -d --build
    exit 0
fi

out="$(docker exec jarvis python -m jarvis update --check)" || { echo "$out"; exit 1; }
echo "$out"
echo "$out" | grep -q "update(s) available" || exit 0

if [ "${1:-}" != "--yes" ]; then
    read -rp "Update now? [y/N] " answer
    [[ "$answer" =~ ^[Yy] ]] || { echo "Okay, not updating."; exit 0; }
fi

result="$(docker exec jarvis python -m jarvis update --yes)"
echo "$result"
if echo "$result" | grep -q "Docker image"; then
    say "This update changes the Docker image: rebuilding"
    docker compose up -d --build
else
    docker restart jarvis >/dev/null
    say "Restarted on the new version"
fi
