#!/bin/bash
# Installation idempotent
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

python3 -m venv "$REPO_DIR/.venv"
source "$REPO_DIR/.venv/bin/activate"
pip install -r "$REPO_DIR/requirements.txt"

sudo tee /etc/systemd/system/habctrl.service > /dev/null <<EOF
[Unit]
Description=HabController
Wants=network-online.target
BindsTo=home-habadm-HabTemp.mount
After=network-online.target home-habadm-HabTemp.mount

[Service]
User=habadm
Group=habadm
SupplementaryGroups=dialout
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/habctrl
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now habctrl
