#!/usr/bin/env bash
# CR-001 graceful lifecycle on the real distro (without terminating it):
#   1. ossctl down --scope workers: draining -> down, sandboxd stopped, gatekeeper keeps running,
#      new jobs refused, approvals untouched, the watchdog does not pause; ossctl up restores.
#   2. ossctl down (scope distro): gatekeeper, proxy, watchdog timer stopped too, shutdown marker
#      written; the next boot gate run classifies it 'graceful' and ends the down; ossctl up.
#   3. ossctl up is refused while killed (exit 3); resume restores.
# Sends the "Going down" / "Back up" summaries to the configured Telegram user only.
# shellcheck source=tests/system/lib.sh disable=SC1091,SC2317
source "$(dirname "$0")/lib.sh"
require_running

OSSCTL=/opt/ossbot/app/.venv/bin/ossctl
DB=/srv/ossbot/gatekeeper/ossbot.db
q() {
  python3 -I -c 'import sqlite3,sys; c=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=10); print(c.execute(sys.argv[2]).fetchone()[0])' "$DB" "$1"
}
lc() { jq -r ".$1 // empty" "$CONTROL/lifecycle" 2>/dev/null; }
inactive() { ! systemctl is-active --quiet "$1"; }
sbx_job() {  # submit a fetch job as root; prints the HTTP status
  curl -s -o /dev/null -w '%{http_code}' --max-time 10 --unix-socket /run/ossbot/sandboxd.sock \
    -H 'content-type: application/json' -X POST http://sandboxd/v1/jobs --data \
    '{"run_id":"lc-test","cid":"c_DRA1NTEST0","profile":"fetch","entrypoint":"fetch","args":{"repo":"owner/repo"},"repo_mode":"rw","timeout_s":60}'
}
invalidated() { q "SELECT COUNT(*) FROM approvals WHERE status = 'invalidated'"; }

# --- 1. workers scope ---------------------------------------------------------------------
inv_before=$(invalidated)
check "ossctl down --scope workers exits 0" "$OSSCTL" down --timeout 5 --scope workers
check "lifecycle flag: down / workers" test "$(lc state)/$(lc scope)" = down/workers
check "sandboxd stopped" inactive ossbot-sandboxd
check "gatekeeper still active" units_active ossbot-gatekeeper ossbot-proxy
st=$(gk GET /v1/control)
if [[ $st == *'"lifecycle":"down"'* && $st == *'"state":"running"'* ]]; then pass "control: running, lifecycle down (not a pause)"
else fail "control view: $st"; fi
check "approvals not invalidated by down" test "$(invalidated)" -eq "$inv_before"
watchdog_tick
refuse "watchdog does not pause while down" test -e "$CONTROL/paused"
check "ossctl up exits 0" "$OSSCTL" up --timeout 120
refuse "lifecycle flag removed by up" test -e "$CONTROL/lifecycle"
check "units active after up" wait_for 60 units_active "${PHASE1_UNITS[@]}"

# --- 2. distro scope (the distro is not terminated here) -----------------------------------
check "ossctl down (distro) exits 0" "$OSSCTL" down --timeout 5
check "gatekeeper, proxy, sandboxd and the watchdog timer stopped" \
  bash -c '! systemctl is-active --quiet ossbot-gatekeeper && ! systemctl is-active --quiet ossbot-proxy && ! systemctl is-active --quiet ossbot-sandboxd && ! systemctl is-active --quiet ossbot-watchdog.timer'
check "shutdown marker: graceful" test "$(jq -r .type "$CONTROL/shutdown-marker" 2>/dev/null)" = graceful
check "lifecycle flag: down / distro" test "$(lc state)/$(lc scope)" = down/distro
# what the next distro start does (the boot gate's lifecycle bookkeeping)
check "boot gate run" /opt/ossbot/bin/ossbot-boot-gate
check "last shutdown classified graceful" test "$(jq -r .type "$CONTROL/last-shutdown" 2>/dev/null)" = graceful
refuse "distro-scope down ended by the boot gate" test -e "$CONTROL/lifecycle"
refuse "shutdown marker consumed" test -e "$CONTROL/shutdown-marker"
check "ossctl up after distro down exits 0" "$OSSCTL" up --timeout 120
check "units active after up" wait_for 60 units_active "${PHASE1_UNITS[@]}"

# --- 3. draining refuses jobs; kill overrides lifecycle --------------------------------------
gk POST /v1/control/drain '{"timeout_s":5,"scope":"workers","reason":"test_lifecycle"}' >/dev/null
code=$(sbx_job)
if [[ $code == 423 ]]; then pass "new sandbox job refused while draining (423)"; else fail "job while draining: HTTP $code"; fi
check "ossctl up lifts draining" "$OSSCTL" up --timeout 60
/opt/ossbot/bin/ossbot-kill --soft --reason test_lifecycle --actor test >/dev/null 2>&1
"$OSSCTL" up --timeout 10 >/dev/null 2>&1
rc=$?
if [[ $rc == 3 ]]; then pass "ossctl up refused while killed (exit 3)"; else fail "ossctl up while killed: exit $rc"; fi
check "resume restores the units" resume_all
check "boot gate ran: last-shutdown recorded" test -e "$CONTROL/last-shutdown"
finish
