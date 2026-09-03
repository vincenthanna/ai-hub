#!/usr/bin/env bash
set -euo pipefail
HERE="$(dirname "${BASH_SOURCE[0]}")"
bash "$HERE/stop.sh" || true
exec bash "$HERE/start.sh"
