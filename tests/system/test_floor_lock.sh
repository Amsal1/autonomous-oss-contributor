#!/usr/bin/env bash
# The root-only public-floor lock (carried criterion C-M1, build-env design §4.7, ADR-0025), ROOT:
#   wsl.exe -d ossbot -u root --cd /mnt/d/Work/autonomous-oss-contributor --exec bash tests/system/test_floor_lock.sh
# 1. The live lock is /etc/ossbot/proxy/floor/.lock, 0600 root:root, in a root:root 0755 directory, and root
#    takes it through the same trust checks every user of the floor runs.
# 2. As ossbox and as gatekeeper (the users that could write /run/ossbot): creating a file in the directory,
#    opening the lock for reading or writing and holding it with flock(1) all fail.
# 3. Two root processes running ossbot-pubip's update routine at once (temp floor dir, stubbed iptables)
#    serialise and end consistent (tests/system/floor_lock_race.py). Nothing live changes.
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"

DIR=/etc/ossbot/proxy/floor
LOCK=$DIR/.lock
PY=${OSSBOT_FIXTURE_PY:-/root/.venvs/ossbot/bin/python}
[[ -x $PY ]] || PY=/opt/ossbot/app/.venv/bin/python
helper() { PYTHONPATH=$REPO/src "$PY" "$REPO/tests/system/floor_lock_race.py" "$@"; }

[[ -d $DIR ]] || { fail "$DIR missing (provision step audit-anchors creates it)"; finish; }
# root takes it (creating it 0600 when missing), through the trust checks of ossbot.common.floor_lock
if helper hold-live; then pass "root takes the live lock"; else fail "root cannot take the live lock"; fi
check "the lock is a regular file, not a symlink" test -f "$LOCK" -a ! -L "$LOCK"
check "the lock is 0600 root:root" test "$(stat -c '%U:%G %a' "$LOCK")" = "root:root 600"
check "its directory is root:root 0755" test "$(stat -c '%U:%G %a' "$DIR")" = "root:root 755"
check "the lock has one link" test "$(stat -c %h "$LOCK")" = 1

for user in ossbox gatekeeper; do
  id "$user" >/dev/null 2>&1 || { fail "user $user missing"; continue; }
  probe=$DIR/.probe-$user-$$
  refuse "$user cannot create a file in $DIR" as "$user" touch "$probe"
  [[ -e $probe ]] && { fail "$user created $probe"; rm -f "$probe"; }
  refuse "$user cannot open the lock for reading" as "$user" cat "$LOCK"
  # shellcheck disable=SC2016  # $1 expands in the child shell (the lock path is its argument)
  refuse "$user cannot open the lock for writing" as "$user" bash -c ': >>"$1"' _ "$LOCK"
  refuse "$user cannot hold the lock (flock -n)" as "$user" flock -n "$LOCK" true
  refuse "$user cannot hold the lock (flock -s -n)" as "$user" flock -s -n "$LOCK" true
  own=$(as "$user" mktemp /tmp/ossbot-floor-probe.XXXXXX)
  refuse "$user cannot replace the lock (rename its own file into the directory)" as "$user" mv -f "$own" "$LOCK"
  rm -f "$own"
  refuse "$user cannot create a lock elsewhere in its place (symlink)" as "$user" ln -sf /tmp/x "$DIR/.lock2"
done
check "the lock is still 0600 root:root afterwards" test "$(stat -c '%U:%G %a' "$LOCK")" = "root:root 600"

# 3. concurrency: prints its own PASS/FAIL lines; any FAIL fails this test
out=$(helper race 2>&1)
rc=$?
printf '%s\n' "$out"
if ((rc == 0)) && ! grep -q '^FAIL' <<<"$out"; then
  pass "two concurrent pubip updaters serialise and end consistent"
else
  fail "the concurrent updater race failed (rc $rc)"
fi
finish
