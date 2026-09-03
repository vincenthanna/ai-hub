#!/usr/bin/env bash
# Start the server detached from this shell; refuses to double-start.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if is_running; then
  echo "[start] already running: pid $(running_pid)" >&2
  exit 1
fi

UV="$(find_uv)"
mkdir -p "$AIHUB_HOME/logs"
cd "$REPO_ROOT"

# setsid detaches the process group so it survives the ssh session ending.
setsid nohup "$UV" run --frozen python -m aihub \
  >>"$LOGFILE" 2>&1 </dev/null &
echo $! >"$PIDFILE"

if wait_for_health 25; then
  echo "[start] up on port $(server_port), pid $(running_pid)"
  exit 0
fi
echo "[start] failed to become healthy in 25s. last log lines:" >&2
tail -n 30 "$LOGFILE" >&2 || true
exit 1
