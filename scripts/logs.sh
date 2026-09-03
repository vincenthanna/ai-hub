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
TARGETS=""
[ -f "$LOGFILE" ] && TARGETS="$TARGETS $LOGFILE"
[ -f "$STDOUT_LOG" ] && TARGETS="$TARGETS $STDOUT_LOG"
[ -n "$TARGETS" ] || { echo "no log files under $AIHUB_HOME/logs" >&2; exit 1; }
# shellcheck disable=SC2086
exec tail $FOLLOW -n "$LINES" $TARGETS
