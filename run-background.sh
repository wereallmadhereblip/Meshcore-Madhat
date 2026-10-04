#!/usr/bin/env bash
# Usage: ./run-background.sh [start|stop|status|restart|enable-boot|disable-boot]
# Runs madhat.py detached and relaunches it whenever it exits.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/meshcore-madhat"
PID_FILE="$STATE_DIR/supervisor.pid"
LOG_FILE="$STATE_DIR/background.log"
PYTHON="$APP_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"
mkdir -p "$STATE_DIR"

running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

supervise() {
  cd "$APP_DIR" || exit 1
  # Prevents the dashboard from opening a browser on every relaunch.
  export MESHC_OPS_RESTARTING=1
  trap 'kill "$child" 2>/dev/null; exit 0' TERM INT
  while true; do
    "$PYTHON" "$APP_DIR/madhat.py" >>"$LOG_FILE" 2>&1 &
    child=$!
    wait "$child"
    echo "$(date '+%F %T') madhat.py exited; restarting in 5s" >>"$LOG_FILE"
    sleep 5
  done
}

case "${1:-start}" in
  start)
    if running; then echo "Already running (PID $(cat "$PID_FILE"))"; exit 0; fi
    export -f supervise
    export APP_DIR LOG_FILE PYTHON
    setsid nohup bash -c supervise >/dev/null 2>&1 &
    echo $! >"$PID_FILE"
    echo "Started in background (PID $!). Log: $LOG_FILE"
    ;;
  stop)
    if running; then
      kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE" && echo "Stopped"
    else
      echo "Not running"
    fi
    ;;
  restart) "$0" stop; sleep 1; "$0" start ;;
  enable-boot)
    # Preferred: system service (works headless on Armbian/Orange Pi and Kali, no login or linger needed).
    SUDO=""
    [[ $EUID -ne 0 ]] && SUDO="sudo"
    RUN_USER="${SUDO_USER:-$USER}"
    if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]] \
       && { [[ -z "$SUDO" ]] || sudo -v; }; then
      $SUDO usermod -aG dialout "$RUN_USER" 2>/dev/null || true
      $SUDO tee /etc/systemd/system/meshcore-madhat.service >/dev/null <<EOF
[Unit]
Description=MeshCore AI Bot Dashboard
After=network-online.target ollama.service
Wants=network-online.target

[Service]
User=$RUN_USER
SupplementaryGroups=dialout
WorkingDirectory=$APP_DIR
Environment=MESHC_OPS_RESTARTING=1
ExecStart=$PYTHON $APP_DIR/madhat.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
      $SUDO systemctl daemon-reload && $SUDO systemctl enable --now meshcore-madhat.service
      echo "Enabled system service. Check: systemctl status meshcore-madhat"
    elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
      UNIT_DIR="$HOME/.config/systemd/user"
      mkdir -p "$UNIT_DIR"
      cat >"$UNIT_DIR/meshcore-madhat.service" <<EOF
[Unit]
Description=MeshCore AI Bot Dashboard
After=network-online.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$PYTHON $APP_DIR/madhat.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
      systemctl --user daemon-reload && systemctl --user enable --now meshcore-madhat.service \
        && sudo -n loginctl enable-linger "$USER" 2>/dev/null \
        || loginctl enable-linger "$USER" 2>/dev/null
      echo "Enabled systemd user service (linger on). Check: systemctl --user status meshcore-madhat"
    elif command -v crontab >/dev/null 2>&1; then
      ( crontab -l 2>/dev/null | grep -vF "$APP_DIR/run-background.sh"; \
        echo "@reboot $APP_DIR/run-background.sh start" ) | crontab -
      echo "Added @reboot cron entry."
    else
      echo "Neither systemd nor cron is available." >&2; exit 1
    fi
    ;;
  disable-boot)
    SUDO=""
    [[ $EUID -ne 0 ]] && SUDO="sudo"
    $SUDO systemctl disable --now meshcore-madhat.service 2>/dev/null
    $SUDO rm -f /etc/systemd/system/meshcore-madhat.service 2>/dev/null
    systemctl --user disable --now meshcore-madhat.service 2>/dev/null
    command -v crontab >/dev/null 2>&1 && ( crontab -l 2>/dev/null | grep -vF "$APP_DIR/run-background.sh" ) | crontab -
    echo "Boot start disabled."
    ;;
  status)
    if running; then echo "Running (PID $(cat "$PID_FILE"))"; else echo "Not running"; exit 1; fi
    ;;
  *) echo "Usage: $0 [start|stop|status|restart|enable-boot|disable-boot]" >&2; exit 2 ;;
esac
