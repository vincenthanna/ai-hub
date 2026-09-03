#!/usr/bin/env bash
# Deploy from the development machine to the hub host over ssh.
#   scripts/deploy.sh [user@host] [remote-path]
set -euo pipefail

HOST="${1:-${AIHUB_DEPLOY_HOST:-yeonhui@192.168.49.48}}"
REMOTE="${2:-${AIHUB_DEPLOY_PATH:-/home/yeonhui/workspace/ai-hub}}"
REPO_URL="${AIHUB_REPO_URL:-https://github.com/vincenthanna/ai-hub.git}"
BRANCH="${AIHUB_BRANCH:-main}"

echo "[deploy] host=$HOST path=$REMOTE branch=$BRANCH"

ssh -o BatchMode=yes "$HOST" bash -s <<REMOTE_SCRIPT
set -euo pipefail
if [ ! -d "$REMOTE/.git" ]; then
  mkdir -p "\$(dirname "$REMOTE")"
  git clone --branch "$BRANCH" "$REPO_URL" "$REMOTE"
else
  cd "$REMOTE"
  git fetch --quiet origin "$BRANCH"
  git checkout --quiet "$BRANCH"
  git reset --hard --quiet "origin/$BRANCH"
fi
cd "$REMOTE"
echo "[deploy] revision \$(git rev-parse --short HEAD)"

# Snapshot before migrations run; a failed upgrade should be recoverable.
if [ -f "\$HOME/.local/share/ai-hub/db/aihub.sqlite3" ]; then
  bash scripts/backup.sh || true
fi

bash scripts/install.sh

# `systemctl --user cat` exits non-zero when the unit does not exist, and unlike
# grepping list-unit-files it cannot be fooled by output formatting. Mixing the
# two restart paths leaves the pidfile process holding the port while systemd
# retries forever, so pick exactly one.
if systemctl --user cat aihub.service >/dev/null 2>&1; then
  echo "[deploy] restarting via systemd --user"
  systemctl --user restart aihub.service
  sleep 4
  systemctl --user is-active aihub.service
else
  echo "[deploy] restarting via pidfile scripts"
  bash scripts/restart.sh
fi
bash scripts/status.sh
REMOTE_SCRIPT

PORT="${AIHUB_PORT:-16001}"
HOSTNAME_ONLY="${HOST##*@}"
echo "[deploy] verifying http://$HOSTNAME_ONLY:$PORT/health from here"
curl -fsS -m 10 "http://$HOSTNAME_ONLY:$PORT/health" && echo && echo "[deploy] ok"
