#!/usr/bin/env bash
PROJECT=/home/nadira/project/rover
SERVICE_FILE=$HOME/.config/systemd/user/rover-mcp.service
LOG_DIR=$PROJECT/logs
DAEMON_BIN=$HOME/.local/bin/rover-daemon

mkdir -p "$LOG_DIR"

cat > "$SERVICE_FILE" <<'UNIT'
[Unit]
Description=Rover Daemon (PR monitor + Telegram bot)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
UNIT

# Inject paths
sed -i "s|^Restart=on-failure|WorkingDirectory=$PROJECT\nExecStart=$DAEMON_BIN\nRestart=on-failure|" "$SERVICE_FILE"
sed -i "/RestartSec=10/a StandardOutput=append:$LOG_DIR/rover-daemon.log\nStandardError=append:$LOG_DIR/rover-daemon.log\nEnvironment=HOME=$HOME" "$SERVICE_FILE"

systemctl --user daemon-reload
systemctl --user restart rover-mcp
sleep 2
systemctl --user status rover-mcp --no-pager
