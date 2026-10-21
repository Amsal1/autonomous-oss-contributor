#!/usr/bin/env bash
# DESIGN §18.10 test_retention_units.sh: both retention timers are enabled and scheduled, journald keeps
# 30 days, `ossctl gc` lists and deletes only old /tmp entries, and pause gates the unit (ConditionResult
# no) and the CLI (exit 3). Leaves the system running with no flags.
# shellcheck source=tests/system/lib.sh disable=SC1091,SC2317
source "$(dirname "$0")/lib.sh"
require_running

OSSCTL=/opt/ossbot/app/.venv/bin/ossctl
old=/tmp/ossbot-gc-test-old.$$
new=/tmp/ossbot-gc-test-new.$$
cleanup() { rm -rf "$old" "$new"; }
trap cleanup EXIT

for t in ossbot-gc.timer ossbot-docker-prune.timer; do
  check "$t enabled" systemctl is-enabled --quiet "$t"
  check "$t active" systemctl is-active --quiet "$t"
  # Capture first: with pipefail, `cmd | grep -q` fails when grep exits early and cmd gets SIGPIPE.
  timers=$(systemctl list-timers --all --no-pager "$t")
  if grep -q "$t" <<<"$timers"; then pass "$t listed by list-timers"; else fail "$t not listed"; fi
done
journald=$(systemd-analyze cat-config systemd/journald.conf)
if grep -qx 'MaxRetentionSec=30d' <<<"$journald"; then
  pass "journald MaxRetentionSec=30d in effect"
else
  fail "journald drop-in missing"
fi

touch "$old" "$new"
touch -d '10 days ago' "$old"
out=$("$OSSCTL" gc --dry-run --only tmp --json)
code=$?
check "gc --dry-run --only tmp exits 0" test "$code" -eq 0
# shellcheck disable=SC2016  # $n is a jq variable
check "dry run lists the old entry" jq -e --arg n "${old#/tmp/}" '.stores.tmp.items | index($n) != null' <<<"$out"
# shellcheck disable=SC2016  # $n is a jq variable
check "dry run does not list the fresh entry" jq -e --arg n "${new#/tmp/}" '.stores.tmp.items | index($n) == null' <<<"$out"
check "dry run deleted nothing" test -e "$old"
full=$("$OSSCTL" gc --dry-run)
code=$?
check "full gc --dry-run exits 0" test "$code" -eq 0
if grep -q '^KEEP  *audit log, open contributions, pending approvals: never selected$' <<<"$full"; then
  pass "full dry run prints the KEEP line"
else
  fail "KEEP line missing"
fi

"$OSSCTL" gc --only tmp >/dev/null
check "gc --only tmp deleted the old entry" test ! -e "$old"
check "gc --only tmp kept the fresh entry" test -e "$new"

reply=$(gk POST /v1/control/pause '{"reason":"test_retention_units"}')
check "paused through the gatekeeper ($reply)" wait_for 10 test -e "$CONTROL/paused"
systemctl start ossbot-gc.service
check "ossbot-gc.service skipped while paused" test "$(systemctl show -p ConditionResult --value ossbot-gc.service)" = no
touch "$old"
touch -d '10 days ago' "$old"
"$OSSCTL" gc --only tmp >/dev/null 2>&1
code=$?
check "gc exits 3 while paused" test "$code" -eq 3
check "nothing deleted while paused" test -e "$old"
check "resume restores every unit" resume_all
check "no flags after resume" test ! -e "$CONTROL/killed" -a ! -e "$CONTROL/paused"
finish
