#!/usr/bin/env bash
# Usage: ./run-background.sh [start|stop|status|restart]
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
  status)
    if running; then echo "Running (PID $(cat "$PID_FILE"))"; else echo "Not running"; exit 1; fi
    ;;
  *) echo "Usage: $0 [start|stop|status|restart]" >&2; exit 2 ;;
esac
