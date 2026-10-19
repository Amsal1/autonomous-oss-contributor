#!/usr/bin/env bash
# DESIGN §15.4 install hygiene (R4-N4, R5-N2), run against a local fixture repository (no network):
# a planted symlink in the hermes-writable home is refused, a tag-object or commit mismatch aborts with
# nothing replaced, a failed `uv sync` rolls back to the previous tree, root-owned skills refuse links,
# and a probe started while the upgrade lock is held is inconclusive ("Hermes is upgrading").
# Uses scratch paths under /home/hermes/.hygiene-test; stops Hermes while it runs and starts it again.
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
require_running
OSSBOT_REPO=$REPO
# shellcheck source=scripts/lib/common.sh disable=SC1091
source "$REPO/scripts/lib/common.sh"
# shellcheck source=scripts/lib/hermes.sh disable=SC1091
source "$REPO/scripts/lib/hermes.sh"

scratch=/home/hermes/.hygiene-test
fixture=$(mktemp -d /var/lib/ossbot/hygiene-fixture.XXXXXX)
# shellcheck disable=SC2317  # invoked by the EXIT trap
cleanup() {
  rm -rf --one-file-system "$scratch" "$fixture" /var/lib/ossbot/staging/hermes-v9.9.9
  systemctl start ossbot-hermes.service ossbot-hermes-probe.socket >/dev/null 2>&1 || true
}
trap cleanup EXIT

# fixture: a repo with an annotated tag and no uv.lock (so `uv sync --frozen` fails)
git init -q -b main "$fixture/src"
printf '[project]\nname = "fixture"\nversion = "0"\n' >"$fixture/src/pyproject.toml"
git -C "$fixture/src" add pyproject.toml
git -C "$fixture/src" -c user.name=t -c user.email=t@example.invalid commit -q -m fixture
git -C "$fixture/src" -c user.name=t -c user.email=t@example.invalid tag -a v9.9.9 -m v9.9.9
tag_object=$(git -C "$fixture/src" rev-parse refs/tags/v9.9.9)
commit=$(git -C "$fixture/src" rev-parse HEAD)
url="file://$fixture/src"

runuser -u hermes -- mkdir -p "$scratch/H/hermes-agent/.git"
printf 'previous-tree\n' | runuser -u hermes -- tee "$scratch/H/hermes-agent/.git/HEAD" >/dev/null
HERMES_DIR=$scratch/H
HERMES_CODE=$scratch/H/hermes-agent
OSSBOT_HERMES_QUIESCED=0
before=$(cat "$HERMES_CODE/.git/HEAD")

# 1. a planted symlink is refused before anything moves
runuser -u hermes -- ln -s /etc "$HERMES_CODE.prev"
out=$( (_hermes_install v9.9.9 "$tag_object" "$commit" "$url") 2>&1)
if [[ $out == *"is a symlink"* ]]; then pass "planted \$D.prev symlink refused"; else fail "symlink not refused: $out"; fi
check "tree unchanged after the refusal" test "$(cat "$HERMES_CODE/.git/HEAD")" = "$before"
check "no hermes process survived the quiesce" bash -c '! pgrep -u hermes'
rm -f "$HERMES_CODE.prev"

# 2. tag-object and commit mismatch abort with nothing replaced
out=$( (_hermes_install v9.9.9 0000000000000000000000000000000000000000 "$commit" "$url") 2>&1)
if [[ $out == *"tag object mismatch"* ]]; then pass "tag object mismatch aborts"; else fail "tag object: $out"; fi
out=$( (_hermes_install v9.9.9 "$tag_object" 1111111111111111111111111111111111111111 "$url") 2>&1)
if [[ $out == *"commit mismatch"* ]]; then pass "commit mismatch aborts"; else fail "commit: $out"; fi
check "tree unchanged after the mismatches" test "$(cat "$HERMES_CODE/.git/HEAD")" = "$before"
check "no staging tree left behind" test ! -e /var/lib/ossbot/staging/hermes-v9.9.9

# 3. a failed uv sync rolls back to the previous tree
out=$( (_hermes_install v9.9.9 "$tag_object" "$commit" "$url") 2>&1)
if [[ $out == *"previous tree was restored"* ]]; then pass "failed uv sync reported"; else fail "sync failure: $out"; fi
check "previous tree restored at \$D" test "$(cat "$HERMES_CODE/.git/HEAD")" = "$before"
check "the failed tree kept as \$D.failed for inspection" test -d "$HERMES_CODE.failed/.git"
check "the failed tree is the fixture commit" grep -q "$commit" "$HERMES_CODE.failed/.git/HEAD"

# 4. root-owned skills refuse a planted link and never become hermes-writable
runuser -u hermes -- mkdir -p "$scratch/H/skills2/x"
runuser -u hermes -- ln -s /etc/shadow "$scratch/H/skills2/x/SKILL.md"
out=$( (hermes_root_dir "$scratch/H/skills2") 2>&1)
if [[ $out == *"link or special file"* ]]; then pass "skills dir with a planted link refused"; else fail "skills link: $out"; fi
rm -f "$scratch/H/skills2/x/SKILL.md"
runuser -u hermes -- touch "$scratch/H/skills2/x/SKILL.md"
(hermes_root_dir "$scratch/H/skills2") >/dev/null 2>&1
check "active skills dir is root:hermes 0750" test "$(stat -c '%U:%G %a' "$scratch/H/skills2")" = "root:hermes 750"
check "a skill file is root:hermes 0640" test "$(stat -c '%U:%G %a' "$scratch/H/skills2/x/SKILL.md")" = "root:hermes 640"
refuse "hermes cannot write an active skill" runuser -u hermes -- bash -c ": >>'$scratch/H/skills2/x/SKILL.md'"
refuse "hermes cannot add a skill" runuser -u hermes -- mkdir "$scratch/H/skills2/y"

# 5. mid-upgrade: a probe while the upgrade lock is held is inconclusive, and nothing is passed
systemctl start ossbot-hermes-probe.socket >/dev/null 2>&1 || true  # the quiesces above stopped it
exec {lockfd}>"$HERMES_UPGRADE_LOCK"
flock -x "$lockfd"
res=$(cd / && /opt/ossbot/app/.venv/bin/python -I -m ossbot.ctl.hermes_probe --force --socket 2>/dev/null | tail -n 1)
flock -u "$lockfd"
exec {lockfd}>&-
if [[ $(jq -r .verdict <<<"$res") == inconclusive && $(jq -r .reason <<<"$res") == "Hermes is upgrading" ]]; then
  pass "probe during an upgrade -> inconclusive (Hermes is upgrading)"
else
  fail "probe during the upgrade: $res"
fi
check "the probe file does not report a pass meanwhile" test "$(jq -r .passed /var/lib/ossbot/probes/hermes-kiro.json)" = false
# the socket path the gatekeeper uses gets the same answer
if systemctl is-active --quiet ossbot-hermes-probe.socket; then
  exec {lockfd}>"$HERMES_UPGRADE_LOCK"
  flock -x "$lockfd"
  res=$(timeout 120 python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect("/run/ossbot/hermes-probe.sock"); s.shutdown(socket.SHUT_WR); sys.stdout.write(s.makefile().read())' 2>/dev/null | tail -n 1)
  flock -u "$lockfd"
  exec {lockfd}>&-
  if [[ $(jq -r .reason <<<"$res" 2>/dev/null) == "Hermes is upgrading" ]]; then
    pass "socket-triggered probe during an upgrade -> Hermes is upgrading"
  else
    fail "socket probe: $res"
  fi
else
  skip "ossbot-hermes-probe.socket not active"
fi
# restore a real result (the next gatekeeper trigger would do the same)
(cd / && /opt/ossbot/app/.venv/bin/python -I -m ossbot.ctl.hermes_probe --force >/dev/null 2>&1) || true
finish
