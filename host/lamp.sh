#!/usr/bin/env bash
# One command for the whole lamp: host + web page, logs in ./logs/.
#
#   ./lamp.sh start [extra host args]   start both, print the URL
#   ./lamp.sh stop                      stop both
#   ./lamp.sh status                    what is running
#   ./lamp.sh logs                      follow both logs
#   ./lamp.sh say "暗一点"               talk to the agent from the CLI
#
# Closing the terminal does not stop it (nohup); use `./lamp.sh stop`.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p logs
PID_HOST=logs/host.pid
PID_WEB=logs/webchat.pid
SOCK="${AMBIENT_SOCK:-/tmp/ambient-lamp.sock}"

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
socket_alive() { [ -S "$SOCK" ] && python3 lampctl.py --socket "$SOCK" ping >/dev/null 2>&1; }
port_busy() { lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; }

cmd="${1:-start}"
[ $# -gt 0 ] && shift

case "$cmd" in
  start)
    # shellcheck disable=SC1091
    source .venv/bin/activate
    if [ -f .env ]; then set -a; source .env; set +a; fi
    [ -n "${DEEPSEEK_API_KEY:-}" ] && AGENT="DeepSeek" || AGENT="off (reflex only)"

    if alive "$PID_HOST"; then
      echo "host     already running (pid $(cat "$PID_HOST"))"
    elif socket_alive; then
      echo "host     a host is already serving $SOCK — not starting a second one"
      echo "         (stop it first, or use --control-socket to give this one another name)"
    else
      nohup python3 ambient_host.py "$@" >>logs/host.log 2>&1 &
      echo $! >"$PID_HOST"
      echo "host     started (pid $(cat "$PID_HOST"))  agent: $AGENT"
    fi

    if alive "$PID_WEB" || port_busy; then
      echo "webchat  already listening on 8765 — not starting a second one"
    else
      nohup python3 webchat.py --port 8765 --open >>logs/webchat.log 2>&1 &
      echo $! >"$PID_WEB"
      echo "webchat  started (pid $(cat "$PID_WEB"))"
    fi

    sleep 1
    echo
    echo "  网页   http://127.0.0.1:8765"
    echo "  日志   ./lamp.sh logs"
    echo "  说话   ./lamp.sh say \"暗一点，慢一点\""
    echo "  停止   ./lamp.sh stop"
    ;;

  stop)
    # pidfiles first, then anything else holding the port / the socket, so that
    # "start, close the laptop, come back, stop" always works.
    for f in "$PID_WEB" "$PID_HOST"; do
      if alive "$f"; then
        kill "$(cat "$f")" 2>/dev/null || true
        echo "stopped $(basename "$f" .pid) (pid $(cat "$f"))"
      fi
      rm -f "$f"
    done
    for pid in $(lsof -nP -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null || true); do
      kill "$pid" 2>/dev/null && echo "stopped webchat (pid $pid)"
    done
    for pid in $(lsof -U 2>/dev/null | grep -F "$SOCK" | awk '{print $2}' | sort -u); do
      kill "$pid" 2>/dev/null && echo "stopped host (pid $pid)"
    done
    ;;

  status)
    if alive "$PID_HOST"; then
      echo "host     running (pid $(cat "$PID_HOST"))"
    elif socket_alive; then
      echo "host     running (not started by lamp.sh)"
    else
      echo "host     not running"
    fi
    if [ -S "$SOCK" ]; then
      python3 lampctl.py observe 2>/dev/null | head -3 || true
    else
      echo "socket   $SOCK missing"
    fi
    if alive "$PID_WEB" || port_busy; then
      echo "webchat  http://127.0.0.1:8765"
    else
      echo "webchat  not running"
    fi
    ;;

  logs)
    touch logs/host.log logs/webchat.log
    tail -n 30 -f logs/host.log logs/webchat.log
    ;;

  say|light|blink|observe|ping)
    # shellcheck disable=SC1091
    source .venv/bin/activate
    exec python3 lampctl.py "$cmd" "$@"
    ;;

  *)
    echo "usage: ./lamp.sh [start|stop|status|logs|say|light|blink|observe]" >&2
    exit 2
    ;;
esac
