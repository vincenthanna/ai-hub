#!/usr/bin/env bash
# Stop the server: SIGTERM, wait, then SIGKILL.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if ! is_running; then
  echo "[stop] not running"
  rm -f "$PIDFILE"
  exit 0
fi

PID="$(running_pid)"
kill -TERM "$PID" 2>/dev/null || true
for _ in $(seq 1 40); do
  kill -0 "$PID" 2>/dev/null || { rm -f "$PIDFILE"; echo "[stop] stopped pid $PID"; exit 0; }
  sleep 0.5
done
echo "[stop] pid $PID did not exit in 20s, sending SIGKILL" >&2
kill -KILL "$PID" 2>/dev/null || true
rm -f "$PIDFILE"
exit 0
