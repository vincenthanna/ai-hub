#!/usr/bin/env bash
# Print the shared auth token. Kept out of install.sh so the secret does not
# land in deploy output, shell history, or a session transcript.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
UV="$(find_uv)"
cd "$REPO_ROOT"
exec "$UV" run --frozen python -m aihub.admin token
