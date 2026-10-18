#!/usr/bin/env bash
# CR-001 item 6c: SIGKILL the services (battery death / force shutdown of a process), let systemd
# restart them, and check reconciliation: gatekeeper healthy with an intact audit chain, sandboxd
# up with no leftover sandbox containers, no outbound action stuck 'executing' and none re-sent.
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

systemctl kill --signal=SIGKILL ossbot-gatekeeper.service ossbot-sandboxd.service ossbot-dockerfilter.service \
  ossbot-runner.service
sleep 2
check "units restarted by systemd after SIGKILL" wait_for 90 units_active ossbot-gatekeeper ossbot-sandboxd ossbot-dockerfilter \
  ossbot-runner
# Runner (FEAT-006): a stage run the killed runner left 'running' is marked interrupted at its next
# start and re-queued (max 3); none may stay running with a stale heartbeat.
stale_runs() { q "SELECT COUNT(*) FROM stage_runs WHERE status = 'running' AND heartbeat_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-120 seconds')"; }
no_stale_runs() { [[ $(stale_runs) -eq 0 ]]; }
check "no stage run left running by the killed runner" wait_for 90 no_stale_runs
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
finish
