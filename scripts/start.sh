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
# stdout goes to its own file: server.log is owned by the rotating handler
# inside the process. The server writes its own pid; $! here is uv's wrapper.
rm -f "$PIDFILE"
setsid nohup "$UV" run --frozen python -m aihub \
  >>"$STDOUT_LOG" 2>&1 </dev/null &
UV_PID=$!

if wait_for_health 40; then
  echo "[start] up on port $(server_port), pid $(running_pid) (uv wrapper $UV_PID)"
  exit 0
fi
echo "[start] failed to become healthy in 40s. last log lines:" >&2
tail -n 40 "$STDOUT_LOG" >&2 || true
tail -n 20 "$LOGFILE" 2>/dev/null >&2 || true
kill -TERM "$UV_PID" 2>/dev/null || true
exit 1
