#!/usr/bin/env bash
# DESIGN §22.6 test_control_perms.sh: only gatekeeper and root can create, rename or unlink in the
# control dir; runner, sandboxd and Hermes users get EACCES. The watchdog detects a mode drift
# (chmod 2770) and kills; the test restores the mode and resumes.
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
require_running

probe="$CONTROL/.permtest.tmp-$$"
install -o root -g osscontrol -m 0640 /dev/null "$probe"
for u in ossrunner ossbox hermes ossnobody; do
  refuse "$u cannot rm in the control dir" as "$u" rm -f "$probe"
  refuse "$u cannot mv in the control dir" as "$u" mv "$probe" "$CONTROL/.permtest-moved"
  refuse "$u cannot create files in the control dir" as "$u" touch "$CONTROL/killed"
done
for u in ossrunner ossbox; do
  check "$u can read flags (group osscontrol)" as "$u" cat "$probe"
done
refuse "hermes cannot read the control dir" as hermes ls "$CONTROL"
check "probe file still there" test -e "$probe"
rm -f "$probe"
check "no flag was created by the attempts" test ! -e "$CONTROL/killed"

# Mode drift -> watchdog kill.
chmod 2770 "$CONTROL"
watchdog_tick
reason=$(flag_reason)
if [[ $reason == perm_drift* ]]; then pass "watchdog killed on chmod 2770 drift ($reason)"; else fail "watchdog did not kill on drift (reason='$reason')"; fi
chmod 2750 "$CONTROL"
check "sandboxd stopped by the kill" wait_for 20 bash -c '! systemctl is-active --quiet ossbot-sandboxd'
check "resume restores every unit" resume_all
check "no flags after resume" test ! -e "$CONTROL/killed" -a ! -e "$CONTROL/paused"
finish
