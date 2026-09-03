#!/usr/bin/env bash
# Register the systemd --user unit so the server survives logout and reboot.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

UV="$(find_uv)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

sed -e "s|__UV__|$UV|" \
    -e "s|%h/workspace/ai-hub|$REPO_ROOT|" \
    "$REPO_ROOT/scripts/aihub.service" > "$UNIT_DIR/aihub.service"

echo "[service] wrote $UNIT_DIR/aihub.service (uv=$UV, workdir=$REPO_ROOT)"

if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
  echo "[service] WARNING: linger is off; the service will stop when your last" >&2
  echo "[service]          session ends. Enable it with:" >&2
  echo "[service]            sudo loginctl enable-linger $USER" >&2
fi

systemctl --user daemon-reload
systemctl --user enable --now aihub.service
sleep 2
systemctl --user --no-pager --lines=20 status aihub.service || true

if wait_for_health 40; then
  echo "[service] healthy on port $(server_port)"
else
  echo "[service] not healthy yet; check: journalctl --user -u aihub.service -n 50" >&2
  exit 1
fi
