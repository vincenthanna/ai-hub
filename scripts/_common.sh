#!/usr/bin/env bash
# Shared helpers for the ai-hub operations scripts.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO_ROOT

: "${AIHUB_HOME:=$HOME/.local/share/ai-hub}"
: "${AIHUB_CONFIG:=$HOME/.config/ai-hub/server.json}"
export AIHUB_HOME AIHUB_CONFIG

PIDFILE="$AIHUB_HOME/server.pid"
LOGFILE="$AIHUB_HOME/logs/server.log"
STDOUT_LOG="$AIHUB_HOME/logs/stdout.log"

# uv is at /snap/bin/uv on ds30 and ~/.local/bin/uv on macOS; find whichever exists.
# Absolute paths come first: `command -v uv` resolves differently under a login
# shell, a non-interactive ssh command, and a systemd user unit, and those can be
# different uv versions.
find_uv() {
  if [ -n "${AIHUB_UV:-}" ] && [ -x "${AIHUB_UV}" ]; then echo "$AIHUB_UV"; return 0; fi
  for candidate in "$HOME/.local/bin/uv" /snap/bin/uv /usr/local/bin/uv "$(command -v uv 2>/dev/null || true)"; do
    [ -n "$candidate" ] && [ -x "$candidate" ] && { echo "$candidate"; return 0; }
  done
  echo "uv not found. Install it from https://docs.astral.sh/uv/ or set AIHUB_UV." >&2
  return 1
}

server_port() {
  if [ -n "${AIHUB_PORT:-}" ]; then echo "$AIHUB_PORT"; return; fi
  if [ -f "$AIHUB_CONFIG" ]; then
    python3 - "$AIHUB_CONFIG" <<'PY' 2>/dev/null || echo 16001
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("port", 16001))
except Exception:
    print(16001)
PY
  else
    echo 16001
  fi
}

is_running() {
  [ -f "$PIDFILE" ] || return 1
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

running_pid() { cat "$PIDFILE" 2>/dev/null || true; }

health_url() { echo "http://127.0.0.1:$(server_port)/health"; }

wait_for_health() {
  local deadline=$(( $(date +%s) + ${1:-20} ))
  local url; url="$(health_url)"
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS -m 3 "$url" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  return 1
}
