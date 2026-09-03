#!/bin/sh
# Announce unread hub messages when a session starts.
#
# A plugin's hooks fire as soon as the plugin is enabled, so the opt-in has to
# live here rather than in hooks.json. Without a config file, or with autoInbox
# off, this exits silently and instantly: an unreachable hub must never slow
# down or pollute a session that is not using it.
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/ai-hub/client.json"
[ -f "$CONFIG" ] || exit 0
grep -q '"autoInbox"[[:space:]]*:[[:space:]]*true' "$CONFIG" || exit 0

PY=$(command -v python3 || true)
[ -n "$PY" ] || exit 0

# Failures go to stderr and never change the exit code; a down server must not
# block the session from starting.
"$PY" "${CLAUDE_PLUGIN_ROOT}/scripts/hub.py" inbox \
  --unread-banner --limit 5 2>/dev/null || true
exit 0
