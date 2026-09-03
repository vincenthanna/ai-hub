#!/usr/bin/env bash
# Daily housekeeping: snapshot, reclaim orphaned blobs, compact indexes.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
UV="$(find_uv)"
cd "$REPO_ROOT"
"$UV" run --frozen python -m aihub.admin backup --keep "${AIHUB_BACKUP_KEEP:-7}"
"$UV" run --frozen python -m aihub.admin gc
"$UV" run --frozen python -m aihub.admin verify
