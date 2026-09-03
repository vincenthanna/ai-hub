#!/usr/bin/env bash
# Consistent database snapshot plus blob-store housekeeping.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
UV="$(find_uv)"
cd "$REPO_ROOT"
"$UV" run --frozen python -m aihub.admin backup --keep "${AIHUB_BACKUP_KEEP:-7}"
