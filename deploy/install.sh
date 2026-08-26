#!/usr/bin/env bash
# Install carriermon as two systemd *user* services (no root needed; the venv and
# data live in this checkout). Paths are derived from where this repo lives.
#
#   deploy/install.sh          # install/refresh units and (re)start them
#
# Prerequisites: .venv created and `.env` filled in (see README).
# To keep the services running without a login session: loginctl enable-linger $USER
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
mkdir -p "$UNIT_DIR"

unit() {  # name, description, subcommand, restart-delay
  cat > "$UNIT_DIR/carriermon-$1.service" <<UNIT
[Unit]
Description=Carrier Monitor — $2
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/carriermon $3
Restart=always
RestartSec=$4

[Install]
WantedBy=default.target
UNIT
}
unit ingest "cloud logger (websocket + poll)" ingest 10
unit web    "web dashboard"                   web    3

systemctl --user daemon-reload
systemctl --user enable --now carriermon-ingest carriermon-web
systemctl --user restart carriermon-ingest carriermon-web
systemctl --user --no-pager --lines=0 status carriermon-ingest carriermon-web | grep -E '^●|Active:'
