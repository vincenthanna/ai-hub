#!/usr/bin/env bash
# Show server logs. -f follows, -n N sets the line count.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

FOLLOW=""; LINES=100
while [ $# -gt 0 ]; do
  case "$1" in
    -f|--follow) FOLLOW="-f"; shift ;;
    -n) LINES="$2"; shift 2 ;;
    *) echo "usage: logs.sh [-f] [-n LINES]" >&2; exit 2 ;;
  esac
done
[ -f "$LOGFILE" ] || { echo "no log file at $LOGFILE" >&2; exit 1; }
exec tail $FOLLOW -n "$LINES" "$LOGFILE"
