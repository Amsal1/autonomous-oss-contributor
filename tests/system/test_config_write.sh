#!/usr/bin/env bash
# DESIGN §22.6 test_config_write.sh (M8): the gatekeeper is the only writer of config/models.yaml
# (PUT /v1/models/<role> succeeds, no temp file left); ossrunner cannot write config/ on drvfs; a
# runner-like unit with the repo read-only gets EROFS. config/models.yaml is restored byte for byte.
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
require_running

models=$REPO/config/models.yaml
backup=$(mktemp)
cp "$models" "$backup"
mode_before=$(stat -c %a "$models")
group_before=$(stat -c %G "$models")
res=$(gk PUT /v1/models/coder '{"model": "default", "effort": "medium"}')
if [[ $res == 200* ]]; then pass "PUT /v1/models/coder as operator -> 200 (gatekeeper wrote models.yaml)"; else fail "PUT -> $res"; fi
check "models.yaml now has coder effort medium" grep -Eq 'coder:.*effort: medium' "$models"
check "mode and group kept after the replace ($mode_before $group_before)" test "$(stat -c "%a %G" "$models")" = "$mode_before $group_before"
check "ossrunner can still read models.yaml (live reader)" as ossrunner cat "$models"
check "no temp file left in config/" test -z "$(find "$REPO/config" -maxdepth 1 -name '.models.yaml.tmp-*')"
res=$(gk PUT /v1/models/coder '{"model": "default"}' ossrunner)
if [[ $res == 403* ]]; then pass "PUT as ossrunner (worker) -> 403"; else fail "PUT as worker -> $res"; fi
cat "$backup" >"$models"
check "models.yaml restored byte for byte" cmp -s "$backup" "$models"
rm -f "$backup"

refuse "ossrunner cannot create config/x on the mount" as ossrunner touch "$REPO/config/x"
refuse "ossrunner cannot modify config/models.yaml" as ossrunner bash -c ": >>'$models'"
out=$(systemd-run --quiet --wait --pipe --uid=ossrunner -p "ReadOnlyPaths=$REPO" touch "$REPO/config/x" 2>&1)
if [[ $out == *"Read-only file system"* ]]; then pass "runner-like unit: touch config/x -> EROFS"; else fail "systemd-run touch: $out"; fi
out=$(systemd-run --quiet --wait --pipe --uid=gatekeeper --gid=gatekeeper -p SupplementaryGroups=osscfg \
  -p "ReadOnlyPaths=$REPO" -p "ReadWritePaths=$REPO/config" touch "$REPO/config/.write-probe.tmp-1" 2>&1)
if [[ -e $REPO/config/.write-probe.tmp-1 ]]; then pass "gatekeeper-like unit can write config/ (osscfg, umask 002)"; else fail "gatekeeper-like write: $out"; fi
rm -f "$REPO/config/.write-probe.tmp-1" "$REPO/config/x"
finish
