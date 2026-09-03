#!/usr/bin/env bash
# Install dependencies, create the data layout, and apply DB migrations.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

UV="$(find_uv)"
echo "[install] repo   : $REPO_ROOT"
echo "[install] uv     : $UV"
echo "[install] home   : $AIHUB_HOME"
echo "[install] config : $AIHUB_CONFIG"
echo "[install] python : $(cat "$REPO_ROOT/.python-version" 2>/dev/null || echo default)"

cd "$REPO_ROOT"
# No fallback to an unfrozen sync: silently re-resolving on the deploy host
# would install a different set of versions than the ones that were tested.
"$UV" sync --frozen --extra dev

mkdir -p "$AIHUB_HOME/db" "$AIHUB_HOME/blobs/tmp" "$AIHUB_HOME/logs"
mkdir -p "$(dirname "$AIHUB_CONFIG")"

# Creates server.json with a fresh token when absent, then applies migrations.
"$UV" run --frozen python -m aihub.admin init

echo "[install] done. token written to $AIHUB_CONFIG (mode 0600)"
echo "[install] show it with: bash scripts/show-token.sh"
