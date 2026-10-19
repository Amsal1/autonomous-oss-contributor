#!/usr/bin/env bash
# CR-001 graceful lifecycle on the real distro (without terminating it):
#   1. ossctl down --scope workers: draining -> down, sandboxd stopped, gatekeeper keeps running,
#      new jobs refused, approvals untouched, the watchdog does not pause; ossctl up restores.
#   1b. the same through the root helper (POST /v1/control/lifecycle-request -> ossbot-lifecycle.path
#      -> ossbot-lifecycle --request; the Telegram /down and /up path): runner, Hermes and sandboxd
#      stop, the gatekeeper answers, up restores; stale, oversize, foreign-owned, distro-scope,
#      unknown-action and symlink requests are consumed and refused (and up while killed, in 3).
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

# --- 1b. workers scope through the root helper (the Telegram /down and /up path, PLAN item 42) ------
REQ=$CONTROL/lifecycle-request
helper_runs() { systemctl show -p ExecMainExitTimestampMonotonic --value ossbot-lifecycle.service; }
helper_done() {  # helper_done RUNS_BEFORE: the helper ran once more and is idle again (a refusal = failed)
  local state
  state=$(systemctl show -p ActiveState --value ossbot-lifecycle.service)
  [[ $(helper_runs) != "$1" ]] && [[ $state == inactive || $state == failed ]]
}
since=$(date +%s)
before=$(helper_runs)
out=$(gk POST /v1/control/lifecycle-request '{"action":"down","timeout_s":5}')
check "lifecycle-request down accepted (202)" test "${out%% *}" = 202
check "the root helper ran the down request" wait_for 180 helper_done "$before"
refuse "the request was consumed" test -e "$REQ"
check "lifecycle flag: down / workers" test "$(lc state)/$(lc scope)" = down/workers
check "runner, Hermes and sandboxd stopped" bash -c '! systemctl is-active --quiet ossbot-runner && ! systemctl is-active --quiet ossbot-hermes && ! systemctl is-active --quiet ossbot-sandboxd'
check "gatekeeper, proxy and the watchdog timer keep running" units_active ossbot-gatekeeper ossbot-proxy ossbot-watchdog.timer
st=$(gk GET /v1/health)
check "the gatekeeper keeps answering" test "${st%% *}" = 200
before=$(helper_runs)
out=$(gk POST /v1/control/lifecycle-request '{"action":"up"}')
check "lifecycle-request up accepted (202)" test "${out%% *}" = 202
check "the root helper ran the up request" wait_for 300 helper_done "$before"
refuse "lifecycle flag removed by the up request" test -e "$CONTROL/lifecycle"
check "units active after the up request" wait_for 60 units_active "${PHASE1_UNITS[@]}"

# Refusals: each request is consumed and changes nothing.
write_req() {  # write_req CONTENT [OWNER]: drop a request the way a writer would (atomic rename)
  local tmp=$CONTROL/.lifecycle-request.test-$$
  printf '%s\n' "$1" >"$tmp" && chown "${2:-root}:osscontrol" "$tmp" && chmod 0640 "$tmp" && mv -f "$tmp" "$REQ"
}
refused_with() {  # refused_with TEXT: the helper's journal since the test start has "refused: TEXT"
  journalctl -u ossbot-lifecycle.service --since "@$since" -o cat --no-pager | grep -F 'refused: ' | grep -qF "$1"
}
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
refused_and_consumed() {  # RUNS_BEFORE TEXT (the journal line can lag the unit state: polled together)
  helper_done "$1" && refused_with "$2" && [[ ! -e $REQ && ! -L $REQ ]]
}
try_refused() {  # try_refused NAME TEXT SETUP...: run SETUP, wait for the helper, expect TEXT
  local name=$1 text=$2 b; shift 2
  b=$(helper_runs)
  "$@"
  if wait_for 60 refused_and_consumed "$b" "$text"; then pass "refused and consumed: $name"
  else fail "request '$name' not refused with '$text'"; fi
}
try_refused "stale ts" "older than 300 s (ts" \
  write_req "{\"action\":\"down\",\"scope\":\"workers\",\"actor\":\"test\",\"ts\":\"$(date -u -d '-10 min' +%Y-%m-%dT%H:%M:%SZ)\",\"timeout_s\":5}"
try_refused "larger than 4 KiB" "larger than 4096 bytes" \
  write_req "{\"action\":\"down\",\"scope\":\"workers\",\"ts\":\"$(now_iso)\",\"pad\":\"$(head -c 5000 /dev/zero | tr '\0' a)\"}"
try_refused "wrong owner" "owned by ossbox" \
  write_req "{\"action\":\"down\",\"scope\":\"workers\",\"ts\":\"$(now_iso)\"}" ossbox
try_refused "scope distro" "scope 'distro'" \
  write_req "{\"action\":\"down\",\"scope\":\"distro\",\"ts\":\"$(now_iso)\"}"
try_refused "unknown action" "action 'reboot'" \
  write_req "{\"action\":\"reboot\",\"scope\":\"workers\",\"ts\":\"$(now_iso)\"}"
try_refused "symlink" "not a regular file" ln -s /etc/hostname "$REQ"
check "the symlink target is untouched" test -s /etc/hostname
check "units unchanged after the refusals" units_active "${PHASE1_UNITS[@]}"
refuse "no lifecycle flag after the refusals" test -e "$CONTROL/lifecycle"

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
out=$(gk POST /v1/control/lifecycle-request '{"action":"up"}')
check "the gatekeeper refuses an up request while killed (409)" test "${out%% *}" = 409
try_refused "up while killed (root check)" "up while killed" \
  write_req "{\"action\":\"up\",\"scope\":\"workers\",\"actor\":\"test\",\"ts\":\"$(now_iso)\"}"
check "resume restores the units" resume_all
check "boot gate ran: last-shutdown recorded" test -e "$CONTROL/last-shutdown"
finish
