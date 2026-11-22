#!/bin/sh
# Jarvis listener for a Raspberry Pi (or any Debian/Ubuntu Linux) with a microphone and a speaker.
#   curl -fsSL https://raw.githubusercontent.com/gtzapper/jarvis/main/listener/install-pi.sh -o install-pi.sh
#   sh install-pi.sh
# It installs into ~/jarvis-listener, asks for Jarvis's address and this device's access code, and starts on boot.
set -e
REPO="https://raw.githubusercontent.com/gtzapper/jarvis/main/listener"
DIR="$HOME/jarvis-listener"
CONFIG="$HOME/.config/jarvis-listener.json"

echo "== Installing system packages (needs sudo)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip libportaudio2 curl

echo "== Downloading the listener to $DIR"
mkdir -p "$DIR"
cd "$DIR"
curl -fsSL "$REPO/jarvis_listener.py" -o jarvis_listener.py
curl -fsSL "$REPO/requirements.txt" -o requirements.txt

echo "== Python packages (a few minutes on a Pi)"
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
venv/bin/pip install -q --no-deps openwakeword

echo "== Setup"
JARVIS_LISTENER_CONFIG="$CONFIG" venv/bin/python jarvis_listener.py setup

echo "== Starting on boot"
sudo tee /etc/systemd/system/jarvis-listener.service >/dev/null <<EOF
[Unit]
Description=Jarvis listener (Hey Jarvis)
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=$(id -un)
SupplementaryGroups=audio
Environment=JARVIS_LISTENER_CONFIG=$CONFIG
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python $DIR/jarvis_listener.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis-listener

echo
echo "Done. Say \"Hey Jarvis\"."
echo "  See what it hears:   journalctl -u jarvis-listener -f"
echo "  Pick another mic:    $DIR/venv/bin/python $DIR/jarvis_listener.py devices   (then edit $CONFIG and: sudo systemctl restart jarvis-listener)"
echo "  Update later:        run this installer again"
