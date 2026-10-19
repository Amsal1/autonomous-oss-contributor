#!/usr/bin/env bash
# CR-001 item 6c: SIGKILL the services (battery death / force shutdown of a process), let systemd
# restart them, and check reconciliation: gatekeeper healthy with an intact audit chain, sandboxd
# up with no leftover sandbox containers, no outbound action stuck 'executing' and none re-sent.
# Covers gatekeeper, sandboxd, dockerfilter and the runner (mid-stage) together, then Hermes alone
# (mid-relay: one roles.test selftest turn, not delivered to Telegram).
# The mid-job sandbox case is tests/sandbox/test_crash_safety.py (ROOTDEV pytest -m sandbox).
# shellcheck source=tests/system/lib.sh disable=SC1091,SC2317
source "$(dirname "$0")/lib.sh"
require_running

DB=/srv/ossbot/gatekeeper/ossbot.db
q() {  # q SQL -> single value, read-only
  python3 -I -c 'import sqlite3,sys; c=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=10); print(c.execute(sys.argv[2]).fetchone()[0])' "$DB" "$1"
}
sbx_health() { curl -sf --max-time 10 --unix-socket /run/ossbot/sandboxd.sock http://sandboxd/v1/health >/dev/null; }
gk_health() { [[ $(gk GET /v1/health | cut -d' ' -f1) == 200 ]]; }

done_before=$(q "SELECT COUNT(*) FROM outbound_actions WHERE status IN ('done','dry_run_done')")
audit_before=$(q "SELECT COUNT(*) FROM audit_events")

# Runner (FEAT-006): seed a stage run in 'running', opened through the worker API as ossrunner,
# so the SIGKILL below lands mid-stage. It goes on the newest terminal contribution: the runner
# never dispatches a terminal item, so nothing runs for it and no GitHub or Kiro call is made;
# the restarted runner must mark it interrupted ("runner restarted"), which is what re-queues a
# live item on the next tick (the re-dispatch itself: tests/e2e/test_e2e_resume.py).
seed_cid=$(q "SELECT COALESCE((SELECT id FROM contributions WHERE state IN ('SKIPPED','FAILED','ABANDONED','DRY_RUN_DONE') AND id NOT IN (SELECT cid FROM stage_runs WHERE status = 'running') ORDER BY updated_at DESC LIMIT 1), '')")
seed_run=""
if [[ -n "$seed_cid" ]]; then
  seeded=$(gk POST "/v1/contributions/$seed_cid/stage-runs" '{"stage":"vet","profile":"research"}' ossrunner)
  if [[ ${seeded%% *} == 201 ]]; then
    seed_run=$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1].split(" ",1)[1])["id"])' "$seeded")
    pass "stage run seeded in 'running' before the kill ($seed_run)"
  else
    fail "could not seed a running stage run: ${seeded:0:200}"
  fi
else
  skip "runner mid-stage case: no terminal contribution to seed a stage run on"
fi

systemctl kill --signal=SIGKILL ossbot-gatekeeper.service ossbot-sandboxd.service ossbot-dockerfilter.service \
  ossbot-runner.service
sleep 2
check "units restarted by systemd after SIGKILL" wait_for 90 units_active ossbot-gatekeeper ossbot-sandboxd ossbot-dockerfilter \
  ossbot-runner
# A stage run the killed runner left 'running' is marked interrupted at its next start and
# re-queued (max 3); none may stay running with a stale heartbeat.
stale_runs() { q "SELECT COUNT(*) FROM stage_runs WHERE status = 'running' AND heartbeat_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-120 seconds')"; }
no_stale_runs() { [[ $(stale_runs) -eq 0 ]]; }
check "no stage run left running by the killed runner" wait_for 90 no_stale_runs
if [[ -n "$seed_run" ]]; then
  seed_state() { q "SELECT status || '/' || COALESCE(error_message, '') FROM stage_runs WHERE id = '$seed_run'"; }
  seed_interrupted() { [[ $(seed_state) == "interrupted/runner restarted" ]]; }
  check "the seeded mid-stage run is interrupted by the restarted runner (re-queue)" wait_for 90 seed_interrupted
fi
check "gatekeeper healthy again" wait_for 60 gk_health
check "sandboxd healthy again" wait_for 60 sbx_health
verify=$(gk GET /v1/audit/verify)
if [[ ${verify%% *} == 200 && $verify == *'"ok":true'* ]]; then pass "audit chain intact after the crash"; else fail "audit verify: $verify"; fi
check "audit grew monotonically (no rows lost)" test "$(q 'SELECT COUNT(*) FROM audit_events')" -ge "$audit_before"
check "no outbound action stuck executing (restart reconcile)" test "$(q "SELECT COUNT(*) FROM outbound_actions WHERE status = 'executing'")" -eq 0
check "no outbound action re-sent" test "$(q "SELECT COUNT(*) FROM outbound_actions WHERE status IN ('done','dry_run_done')")" -eq "$done_before"
check "no sandbox containers left" test -z "$(docker ps -aq --filter label=ossbot.sandbox=1)"
check "firewall unchanged" /opt/ossbot/bin/ossbot-firewall --check
refuse "no pause/kill flag written by the crash" test -e "$CONTROL/killed" -o -e "$CONTROL/paused"

# Hermes (FEAT-007, PLAN item 42): SIGKILL ossbot-hermes mid-relay. One selftest turn on roles.test
# (deliver false: nothing goes to Telegram) is streaming when Hermes and its kiro-cli die; the turn
# must end failed (no row left streaming, the gatekeeper keeps answering), systemd restarts Hermes,
# and the next turn passes the relay gate again (probe result + a fresh /v1/toolsets surface check:
# a HermesUnavailable invalidates the cached surface).
selftest() {  # selftest OUTFILE: POST /v1/chat/selftest as root (operator), up to 300 s
  curl -s -o "$1" -w '%{http_code}' --max-time 300 --unix-socket "$GK_SOCK" -X POST \
    -H 'content-type: application/json' --data '{"deliver":false}' http://ossbot/v1/chat/selftest 2>/dev/null
}
streaming() { [[ $(q "SELECT COUNT(*) FROM chat_turns WHERE status = 'streaming'") -ge 1 ]]; }
no_streaming() { [[ $(q "SELECT COUNT(*) FROM chat_turns WHERE status = 'streaming'") -eq 0 ]]; }
json_field() { python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]))' "$1" "$2" 2>/dev/null; }
turns_since=$(date -u +%Y-%m-%dT%H:%M:%S)  # turn ids are random: count by start time
first=$(mktemp)
selftest "$first" >"$first.code" &
st_pid=$!
if wait_for 60 streaming; then
  pass "a chat turn is streaming before the kill"
  systemctl kill --signal=SIGKILL ossbot-hermes.service
  wait "$st_pid"
  code=$(cat "$first.code")
  if [[ $code == 200 && $(json_field "$first" status) == failed ]] || [[ $code == 503 ]]; then
    pass "the cut turn ends failed (HTTP $code, $(json_field "$first" error | head -c 80))"
  else
    fail "the cut turn: HTTP $code $(head -c 200 "$first")"
  fi
  check "no chat turn left streaming" wait_for 30 no_streaming
  check "the gatekeeper keeps answering" gk_health
  check "Hermes restarted by systemd" wait_for 90 units_active ossbot-hermes
  second=$(mktemp)
  ok=0
  for _ in 1 2 3 4 5 6; do  # Hermes needs a few seconds before its API server listens
    sleep 10
    code=$(selftest "$second")
    if [[ $code == 200 && $(json_field "$second" status) == "done" ]]; then ok=1; break; fi
  done
  if ((ok)); then pass "the next turn passes the relay gate again (done, $(json_field "$second" latency_s) s)"
  else fail "the next turn after the restart: HTTP $code $(head -c 200 "$second")"; fi
  check "both turns recorded (no lost row)" test "$(q "SELECT COUNT(*) FROM chat_turns WHERE started_at >= '$turns_since'")" -ge 2
  rm -f "$second"
else
  wait "$st_pid"
  # the AC needs this case to pass: a closed relay (tier, probe, Hermes down) fails the run, never skips
  fail "Hermes mid-relay case could not run: no chat turn started (relay closed? $(head -c 120 "$first"))"
fi
rm -f "$first" "$first.code"
finish
