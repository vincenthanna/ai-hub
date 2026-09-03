#!/usr/bin/env bash
# Exit code: 0 healthy, 1 not running, 2 running but degraded/unreachable.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if systemctl --user is-active aihub.service >/dev/null 2>&1; then
  PID="$(systemctl --user show -p MainPID --value aihub.service 2>/dev/null || echo systemd)"
  echo "[status] managed by systemd --user (MainPID $PID)"
elif is_running; then
  PID="$(running_pid)"
else
  echo "[status] not running (no systemd unit active, no live pid in $PIDFILE)"
  exit 1
fi
BODY="$(curl -fsS -m 5 "$(health_url)" 2>/dev/null || true)"
if [ -z "$BODY" ]; then
  echo "[status] pid $PID alive but $(health_url) is unreachable"
  exit 2
fi
echo "[status] pid $PID, port $(server_port)"
echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
echo "$BODY" | grep -q '"status": *"ok"' && exit 0 || exit 2
