#!/usr/bin/env bash
# DESIGN §22.6 test_firewall_failclosed.sh (M6): with OSSBOT-OUT flushed, a unit restart fails on
# its ExecStartPre --check (the runner and sandboxd carry the same ExecStartPre),
# and the watchdog writes killed with reason firewall_drift. Re-apply + resume starts it again.
# A Docker restart re-applies OSSBOT-FWD through ossbot-firewall-docker (PartOf=docker.service).
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
require_running

iptables -w -F OSSBOT-OUT
refuse "firewall --check fails with OSSBOT-OUT flushed" /opt/ossbot/bin/ossbot-firewall --check
for unit in ossbot-runner ossbot-sandboxd; do
  refuse "systemctl restart $unit fails" systemctl restart "$unit"
  if journalctl -u "$unit" -n 30 --no-pager -o cat | grep -q 'ossbot-firewall --check'; then
    pass "$unit start refused by the --check ExecStartPre (journal)"
  else
    fail "journal of $unit does not show the --check message"
  fi
done
watchdog_tick
reason=$(flag_reason)
if [[ $reason == firewall_drift* ]]; then pass "watchdog killed ($reason)"; else fail "watchdog reason '$reason'"; fi
/opt/ossbot/bin/ossbot-firewall --apply --part host >/dev/null
check "firewall --check passes after re-apply" /opt/ossbot/bin/ossbot-firewall --check
check "resume starts every unit again" resume_all

# PartOf: a Docker restart re-applies the DOCKER-USER chain.
systemctl restart docker
check "ossbot-firewall-docker active after docker restart" wait_for 60 systemctl is-active --quiet ossbot-firewall-docker
check "DOCKER-USER jumps to OSSBOT-FWD at position 1" bash -c "iptables -w -S DOCKER-USER | sed -n 2p | grep -qx -- '-A DOCKER-USER -j OSSBOT-FWD'"
check "firewall --check passes after docker restart" /opt/ossbot/bin/ossbot-firewall --check
check "units recover after the docker restart" wait_for 120 units_active "${PHASE1_UNITS[@]}"
check "no kill flag after the docker restart" test ! -e "$CONTROL/killed"
finish
