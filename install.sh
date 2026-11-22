#!/usr/bin/env bash
# Jarvis installer for a Docker host: Debian/Ubuntu, a Proxmox LXC/VM, or Unraid.
#
#   git clone https://github.com/gtzapper/jarvis.git /opt/jarvis && /opt/jarvis/install.sh
#
# Private repo: GITHUB_TOKEN=<token> ./install.sh   (the token is also saved so the in-app updater can pull)
# Options (environment): JARVIS_DIR, JARVIS_REPO, JARVIS_PORT, PUID, PGID, TZ
set -euo pipefail

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "Run as root (sudo $0)."

UNRAID=0
[ -f /etc/unraid-version ] && UNRAID=1
REPO="${JARVIS_REPO:-https://github.com/gtzapper/jarvis.git}"
if [ "$UNRAID" = 1 ]; then
    DIR="${JARVIS_DIR:-/mnt/user/appdata/jarvis}"; PUID="${PUID:-99}"; PGID="${PGID:-100}"
else
    DIR="${JARVIS_DIR:-/opt/jarvis}"; PUID="${PUID:-1000}"; PGID="${PGID:-1000}"
fi
PORT="${JARVIS_PORT:-8765}"
TZ_VALUE="${TZ:-$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo UTC)}"

command -v git >/dev/null || die "git isn't installed. Debian/Ubuntu: apt install -y git"
command -v docker >/dev/null || die "Docker isn't installed. Debian/Ubuntu: curl -fsSL https://get.docker.com | sh"
if ! docker compose version >/dev/null 2>&1; then
    [ "$UNRAID" = 1 ] && die "Install the 'Compose Manager' plugin from Unraid's Apps tab first."
    die "Docker Compose v2 (the 'docker compose' command) is missing."
fi

# Run from inside an existing checkout? Use that one.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -d "$HERE/.git" ] && [ -f "$HERE/docker-compose.yml" ]; then
    DIR="$HERE"
fi

auth_args=()
if [ -n "${GITHUB_TOKEN:-}" ]; then
    auth_args=(-c "http.extraheader=AUTHORIZATION: basic $(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')")
fi

if [ ! -d "$DIR/.git" ]; then
    say "Downloading Jarvis into $DIR"
    mkdir -p "$(dirname "$DIR")"
    git "${auth_args[@]}" clone "$REPO" "$DIR" || die "Download failed. For a private repo run: GITHUB_TOKEN=<token> $0"
fi
cd "$DIR"
mkdir -p config

# Settings for docker compose (separate from Jarvis's own config/.env).
if [ ! -f .env ]; then
    say "Writing docker compose settings (.env): user $PUID:$PGID, port $PORT, time zone $TZ_VALUE"
    printf 'PUID=%s\nPGID=%s\nJARVIS_PORT=%s\nTZ=%s\n' "$PUID" "$PGID" "$PORT" "$TZ_VALUE" > .env
else
    say "Keeping existing docker compose settings in $DIR/.env"
    PUID="$(sed -n 's/^PUID=//p' .env)"; PUID="${PUID:-1000}"
    PGID="$(sed -n 's/^PGID=//p' .env)"; PGID="${PGID:-1000}"
    PORT="$(sed -n 's/^JARVIS_PORT=//p' .env)"; PORT="${PORT:-8765}"
fi
# A private secret for the optional web search container (only added once).
if ! grep -q '^SEARXNG_SECRET=' .env 2>/dev/null; then
    printf 'SEARXNG_SECRET=%s\n' "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> .env
fi

# Let the in-app updater pull from a private repo too.
if [ -n "${GITHUB_TOKEN:-}" ] && ! grep -q '^JARVIS_GIT_TOKEN=' config/.env 2>/dev/null; then
    printf 'JARVIS_GIT_TOKEN=%s\n' "$GITHUB_TOKEN" >> config/.env
    chmod 600 config/.env
fi

# The container runs as PUID:PGID and must own both the code (to update it) and config/.
chown -R "$PUID:$PGID" "$DIR"

say "Building and starting Jarvis (the first build takes a minute or two)"
docker compose up -d --build

say "Waiting for Jarvis to start"
password=""
for _ in $(seq 1 45); do
    password="$(docker logs jarvis 2>&1 | sed -n 's/.*dashboard password is *//p' | tail -1)"
    if [ -n "$password" ] || docker logs jarvis 2>&1 | grep -q "Jarvis dashboard:"; then break; fi
    sleep 2
done

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
say "Jarvis is running: http://${ip:-this-host}:$PORT"
if [ -n "$password" ]; then
    echo "    Dashboard password: $password"
    echo "    (saved in $DIR/config/.env; change it in Setup > Web dashboard)"
else
    echo "    Log in with your existing dashboard password."
fi
echo "    Update later from the dashboard (Setup > Jarvis version), or run: $DIR/update.sh"
