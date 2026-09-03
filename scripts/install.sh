#!/usr/bin/env bash
# Install dependencies, create the data layout, and apply DB migrations.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

UV="$(find_uv)"
echo "[install] repo   : $REPO_ROOT"
echo "[install] uv     : $UV"
echo "[install] home   : $AIHUB_HOME"
echo "[install] config : $AIHUB_CONFIG"

cd "$REPO_ROOT"
if [ -f uv.lock ]; then
  "$UV" sync --frozen --extra dev || "$UV" sync --extra dev
else
  "$UV" sync --extra dev
fi

mkdir -p "$AIHUB_HOME/db" "$AIHUB_HOME/blobs/tmp" "$AIHUB_HOME/logs"
mkdir -p "$(dirname "$AIHUB_CONFIG")"

# Creates server.json with a fresh token when absent, then applies migrations.
"$UV" run --frozen python -m aihub.admin init 2>/dev/null || "$UV" run python -m aihub.admin init

echo "[install] done. token: $("$UV" run python -m aihub --print-token)"
