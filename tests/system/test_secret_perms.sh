#!/usr/bin/env bash
# DESIGN §22.6 test_secret_perms.sh: every service reads only its own env file; nobody else reads
# gatekeeper.env; the secrets dir cannot be listed by non-root users. Values are never printed.
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"

S=/etc/ossbot/secrets
declare -A OWN=([gatekeeper]=gatekeeper.env [ossrunner]=runner.env [ossbox]=sandboxd.env [hermes]=hermes.env)
for u in ossrunner ossbox hermes ossnobody; do
  refuse "$u cannot read gatekeeper.env" as "$u" cat "$S/gatekeeper.env"
done
for u in "${!OWN[@]}"; do
  check "$u reads its own ${OWN[$u]}" as "$u" cat "$S/${OWN[$u]}"
  for f in gatekeeper.env runner.env sandboxd.env hermes.env; do
    [[ $f == "${OWN[$u]}" ]] && continue
    refuse "$u cannot read $f" as "$u" cat "$S/$f"
  done
done
for f in gatekeeper.env runner.env sandboxd.env hermes.env; do
  refuse "ossnobody cannot read $f" as ossnobody cat "$S/$f"
  refuse "ossadmin cannot read $f" as ossadmin cat "$S/$f"
done
for u in ossnobody ossadmin ossrunner; do
  refuse "$u cannot list $S" as "$u" ls "$S"
done
check "dir mode root:root 751" test "$(stat -c '%U:%G %a' "$S")" = "root:root 751"
for f in gatekeeper:gatekeeper runner:ossrunner sandboxd:ossbox hermes:hermes; do
  check "${f%%:*}.env is root:${f#*:} 640" test "$(stat -c '%U:%G %a' "$S/${f%%:*}.env")" = "root:${f#*:} 640"
done
finish
