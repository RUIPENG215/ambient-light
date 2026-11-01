#!/usr/bin/env bash
# End-to-end smoke test: fake Feather on a pty + real host, no hardware.
set -euo pipefail

cd "$(dirname "$0")/.."

OUT=$(mktemp)
ERR=$(mktemp)
HOST_LOG=$(mktemp)
cleanup() {
  kill "${SIM_PID:-}" 2>/dev/null || true
  wait "${SIM_PID:-}" 2>/dev/null || true
  rm -f "$OUT" "$ERR" "$HOST_LOG"
}
trap cleanup EXIT

python3 sim_feather.py --pty >"$OUT" 2>"$ERR" &
SIM_PID=$!

PORT=""
for _ in $(seq 1 50); do
  PORT=$(sed -n 's/^PTY=//p' "$OUT" | head -1)
  [ -n "$PORT" ] && break
  sleep 0.1
done

if [ -z "$PORT" ]; then
  echo "FAIL: simulator did not start"
  cat "$ERR"
  exit 1
fi
echo "pty: $PORT"

python3 ambient_host.py \
  --port "$PORT" \
  --no-llm \
  --sim-audio \
  --calibrate 1 \
  --duration 6 \
  --llm-min-interval 1 \
  >"$HOST_LOG" 2>&1 || {
    echo "FAIL: host exited non-zero"
    cat "$HOST_LOG"
    exit 1
  }

sleep 0.5
RX=$(grep -o 'rx_l=[0-9]*' "$ERR" | tail -1 | cut -d= -f2)
if [ -z "${RX:-}" ] || [ "$RX" -lt 10 ]; then
  echo "FAIL: simulator received ${RX:-0} level lines"
  tail -20 "$ERR"
  exit 1
fi

if ! grep -q 'mood ->' "$HOST_LOG"; then
  echo "FAIL: host never emitted a mood command"
  tail -20 "$HOST_LOG"
  exit 1
fi

echo "PASS: smoke test ($RX level lines reached the lamp simulator)"
