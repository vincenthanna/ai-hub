#!/usr/bin/env bash
# Audit every condition the server needs to come back on its own after a reboot,
# without rebooting anything. Exits non-zero if any of them is missing.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

FAILED=0
ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  MISS  %s\n     -> %s\n' "$1" "$2"; FAILED=$((FAILED + 1)); }

echo "reboot-survival audit for aihub.service"

# 1. Without linger, the user manager only runs while a session is open, so a
#    reboot with nobody logged in leaves the service down.
if [ -e "/var/lib/systemd/linger/$USER" ]; then
  ok "linger is recorded on disk (/var/lib/systemd/linger/$USER)"
else
  bad "linger is not enabled for $USER" "sudo loginctl enable-linger $USER"
fi

# 2. Enabled means the symlink exists; that symlink is what default.target
#    follows at boot.
if systemctl --user is-enabled aihub.service >/dev/null 2>&1; then
  ok "unit is enabled"
else
  bad "unit is not enabled" "systemctl --user enable aihub.service"
fi

WANTS="$HOME/.config/systemd/user/default.target.wants/aihub.service"
if [ -L "$WANTS" ]; then
  ok "default.target wants the unit"
else
  bad "no symlink in default.target.wants" "systemctl --user enable aihub.service"
fi

# 3. Restart=always plus no start-rate limit, or a transient failure at boot
#    leaves the unit failed for good.
RESTART="$(systemctl --user show aihub.service -p Restart --value 2>/dev/null)"
[ "$RESTART" = "always" ] && ok "Restart=always" \
  || bad "Restart is '$RESTART'" "set Restart=always in the unit"

LIMIT="$(systemctl --user show aihub.service -p StartLimitIntervalUSec --value 2>/dev/null)"
if [ "$LIMIT" = "0" ] || [ "$LIMIT" = "infinity" ]; then
  ok "start-rate limit disabled (never gives up)"
else
  bad "StartLimitIntervalUSec=$LIMIT; systemd will stop retrying after a burst" \
      "add StartLimitIntervalSec=0 to [Unit] and reinstall"
fi

# 4. A network-mounted home may not be there when the user manager starts.
FS="$(findmnt -no FSTYPE --target "$AIHUB_HOME" 2>/dev/null || echo unknown)"
case "$FS" in
  nfs*|cifs|smb*|fuse*) bad "data root is on $FS, which may mount after the user manager starts" \
                            "add RequiresMountsFor= to the unit, or move AIHUB_HOME to local disk" ;;
  unknown) bad "cannot determine the filesystem for $AIHUB_HOME" "check the path exists" ;;
  *) ok "data root is on a local filesystem ($FS)" ;;
esac

# 5. The unit's PATH must contain the uv that install-service.sh baked in.
UVPATH="$(systemctl --user show aihub.service -p ExecStart --value 2>/dev/null | sed -n 's/.*path=\([^ ;]*\).*/\1/p')"
if [ -n "$UVPATH" ] && [ -x "$UVPATH" ]; then
  ok "ExecStart points at an existing uv ($UVPATH)"
else
  bad "ExecStart uv path is missing or not executable: ${UVPATH:-<unset>}" \
      "re-run scripts/install-service.sh"
fi

# 6. A clean stop must not leave the unit in 'failed', or status checks and
#    monitoring cannot distinguish a deliberate stop from a crash.
SUCCESS="$(systemctl --user show aihub.service -p SuccessExitStatus --value 2>/dev/null)"
case "$SUCCESS" in
  *143*) ok "clean SIGTERM stop is treated as success" ;;
  *) bad "SuccessExitStatus does not cover 143" \
         "add 'SuccessExitStatus=143 SIGTERM' to [Service] and reinstall" ;;
esac

# 7. Currently up?
if systemctl --user is-active aihub.service >/dev/null 2>&1; then
  ok "unit is active right now (MainPID $(systemctl --user show -p MainPID --value aihub.service))"
else
  bad "unit is not active" "systemctl --user start aihub.service"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "all conditions met: the server will come back on its own after a reboot"
  exit 0
fi
echo "$FAILED condition(s) missing"
exit 1
