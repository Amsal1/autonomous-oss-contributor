#!/usr/bin/env bash
# DESIGN §15.5 tier switching (PLAN item 36): locked -> self_improve -> locked -> back to the starting
# tier through the gatekeeper API (operator) and the root path unit: /etc/hermes/config.yaml is
# re-rendered, Hermes restarted, the Hermes-side probe re-run and the /v1/toolsets surface matches the
# tier; the active skills dir stays read-only for Hermes; the staging scan runs as root.
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
require_running
RESULT=/var/lib/ossbot/probes/hermes-surface.json
start_tier=$(gk GET /v1/control/hermes-tier | cut -d' ' -f2- | jq -r .tier)
[[ $start_tier =~ ^(locked|research|self_improve)$ ]] || { fail "cannot read the current tier"; finish; }

# switch_to TIER: POST the tier, wait for the root apply to record it (fresh checked_at)
switch_to() {
  local tier=$1 before res
  before=$(jq -r '.checked_at // ""' "$RESULT" 2>/dev/null || true)
  # one apply at a time: a previous run must be fully finished before the next flag write
  wait_for 300 bash -c '! systemctl is-active --quiet ossbot-hermes-tier.service'
  res=$(gk POST /v1/control/hermes-tier "{\"tier\": \"$tier\"}")
  if [[ $res != 200* ]]; then fail "POST tier $tier -> $res"; return 1; fi
  sleep 1
  wait_for 600 bash -c "[[ \$(jq -r .tier '$RESULT') == '$tier' && \$(jq -r .checked_at '$RESULT') != '$before' ]]" \
    || { fail "tier $tier was not applied within 600 s"; return 1; }
}

expect_tier() {
  local tier=$1 want
  case $tier in
    locked) want='["memory","todo","session_search","ossbot"]' ;;
    research) want='["memory","todo","session_search","ossbot","web"]' ;;
    self_improve) want='["memory","todo","session_search","ossbot","web","skills"]' ;;
  esac
  local have
  have=$(sed -n 's/^  api_server: \[\(.*\)\]$/\1/p' /etc/hermes/config.yaml | tr -d ' ' | jq -Rc 'split(",")')
  if [[ $have == "$want" ]]; then pass "$tier: managed config lists $want"; else fail "$tier: managed config lists $have"; fi
  check "$tier: managed config is root:root 0644" test "$(stat -c '%U:%G %a' /etc/hermes/config.yaml)" = "root:root 644"
  check "$tier: surface check ok" test "$(jq -r .surface.outcome "$RESULT")" = ok
  check "$tier: Hermes-side probe re-run and passed" test "$(jq -r .probe.verdict "$RESULT")" = pass
  for t in terminal code_execution file browser delegation cronjob; do
    if grep -E 'api_server: \[' /etc/hermes/config.yaml | grep -qw "$t"; then fail "$tier: $t enabled"; fi
  done
  pass "$tier: terminal/code_execution/file/browser/delegation/cronjob stay off"
}

switch_to locked && expect_tier locked
switch_to self_improve && expect_tier self_improve
switch_to locked && expect_tier locked
if [[ $start_tier != locked ]]; then switch_to "$start_tier" && pass "restored tier $start_tier"; fi
check "relay: the probe file holds a fresh pass again" test "$(jq -r .passed /var/lib/ossbot/probes/hermes-kiro.json)" = true

# the skill gate's fixed points on the real install
check "active skills dir is root:hermes 0750" test "$(stat -c '%U:%G %a' /home/hermes/.hermes/skills)" = "root:hermes 750"
refuse "hermes cannot create a skill in the active dir" runuser -u hermes -- mkdir /home/hermes/.hermes/skills/x
check "root staging scan runs" bash -c 'cd / && /opt/ossbot/app/.venv/bin/python -I -m ossbot.ctl.skills_root scan'
check "pending.json is root:gatekeeper 0640" test "$(stat -c '%U:%G %a' /var/lib/ossbot/skills/pending.json)" = "root:gatekeeper 640"
check "no skill drift" bash -c 'cd / && /opt/ossbot/app/.venv/bin/python -I -m ossbot.ctl.skills_root drift'
check "managed config: skills.write_approval true" grep -Eq '^  write_approval: true' /etc/hermes/config.yaml
finish
