#!/usr/bin/env bash
# Records what iptables 1.8.10 (nf_tables) really prints for the rendered firewall plan (build-env
# design §4.4 item 5, L-6): tests/unit/ctl/test_firewall_build_env.py::test_check_matches_recorded_listing
# runs check_chain against this listing, so the canonical render forms and the --check normaliser are
# proven against the real tool, not against a hand-written imitation.
#
# The live rules are never touched: the plan (rendered from the repo's src/, `firewall --print --part all`)
# is applied in a scratch network namespace (nf_tables tables are per netns) that gets its own DOCKER-USER
# chain and FORWARD jump, both tables of both families are listed with `-S`, and the netns is deleted in a
# trap. Re-run it whenever firewall_plan changes and commit the fixture.
#   wsl.exe -d ossbot -u root --cd /mnt/d/Work/autonomous-oss-contributor --exec bash tests/system/capture_firewall_fixture.sh
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"

OUT=$REPO/tests/fixtures/firewall/iptables-S-1.8.10-nft.txt
SERVICE_USERS=(ossrunner hermes gatekeeper ossbox ossnobody)  # = render.SERVICE_USERS
NETNS=ossbot-fwfixture-$$
PY=${OSSBOT_FIXTURE_PY:-/root/.venvs/ossbot/bin/python}
[[ -x $PY ]] || PY=/opt/ossbot/app/.venv/bin/python
SCRIPT=$(mktemp)
TMP_OUT=$(mktemp)
cleanup() {
  ip netns delete "$NETNS" >/dev/null 2>&1
  rm -f "$SCRIPT" "$TMP_OUT"
}
trap cleanup EXIT

# live_ossbot: the OSSBOT lines of the host netns (must be identical before and after)
live_ossbot() {
  local fam table
  for fam in iptables ip6tables; do
    for table in filter mangle; do
      "$fam" -w -t "$table" -S 2>/dev/null | grep OSSBOT
    done
  done
}
before=$(live_ossbot)

if ! PYTHONPATH=$REPO/src "$PY" -m ossbot.ctl.render firewall --print --part all >"$SCRIPT"; then
  fail "rendering the plan from $REPO/src failed"
  finish
fi
check "the rendered plan has chains" grep -q -- ' -N OSSBOT-' "$SCRIPT"

ip netns add "$NETNS" || { fail "ip netns add $NETNS failed"; finish; }
ip netns exec "$NETNS" iptables -w -N DOCKER-USER
ip netns exec "$NETNS" iptables -w -A FORWARD -j DOCKER-USER
if ip netns exec "$NETNS" bash -e "$SCRIPT"; then
  pass "the rendered plan applies in the scratch netns"
else
  fail "the rendered plan does not apply in the scratch netns"
  finish
fi

ns=$(awk '$1 == "nameserver" {print $2; exit}' /etc/resolv.conf)
{
  printf '# Recorded by tests/system/capture_firewall_fixture.sh on %s in a scratch netns (never the live rules).\n' \
    "$(date -u +%Y-%m-%d)"
  # shellcheck disable=SC2016  # ${Version} is a dpkg-query field, not a shell expansion
  printf '# %s (%s)\n' "$(iptables --version)" "$(dpkg-query -W -f '${Version}' iptables 2>/dev/null)"
  printf '# ns %s\n' "$ns"
  for user in "${SERVICE_USERS[@]}"; do printf '# uid %s %s\n' "$user" "$(id -u "$user")"; done
  for fam in iptables ip6tables; do
    for table in filter mangle; do
      printf '## %s %s\n' "$fam" "$table"
      ip netns exec "$NETNS" "$fam" -w -t "$table" -S
    done
  done
} >"$TMP_OUT" || { fail "listing the scratch netns failed"; finish; }

every_chain_listed() {
  local c
  while read -r c; do
    grep -q -- "^-N $c\$" "$TMP_OUT" || return 1
  done < <(sed -n 's/.* -N \(OSSBOT-[A-Z-]*\) .*/\1/p' "$SCRIPT" | sort -u)
}
check "the listing names every rendered chain" every_chain_listed
mkdir -p "$(dirname "$OUT")"
cp "$TMP_OUT" "$OUT"
# the repo is a drvfs mount with metadata: hand the file (and its directory) to the repo's owner
chmod 0644 "$OUT" && chmod 0755 "$(dirname "$OUT")"
chown --reference="$REPO/tests/fixtures" "$OUT" "$(dirname "$OUT")"
pass "wrote $OUT ($(wc -l <"$OUT") lines)"

if [[ $(live_ossbot) == "$before" ]]; then
  pass "the live OSSBOT rules are unchanged"
else
  fail "the live OSSBOT rules changed while recording (this helper never touches them; check for a concurrent apply)"
fi
finish
