#!/usr/bin/env bash
# DESIGN §22.6 test_audit_anchor.sh (§12.7, N7): an anchor is appended on demand through
# /run/ossbot/anchor.sock (what the gatekeeper does after every outbound write) and on every 10th
# watchdog tick (5 min); chattr +a blocks truncation and rewrites; audit verify passes.
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
require_running

log=/srv/ossbot/logs/audit-anchors.log
before=$(wc -l <"$log")
reply=$(as gatekeeper python3 -I -c '
import socket
s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect("/run/ossbot/anchor.sock"); s.sendall(b"\n")
print(s.recv(16).decode().strip())')
if [[ $reply == OK ]]; then pass "anchor socket replied OK to the gatekeeper"; else fail "anchor socket reply '$reply'"; fi
check "one anchor line appended on demand" test "$(wc -l <"$log")" -eq $((before + 1))
check "the line names the current audit head" bash -c "tail -n 1 '$log' | jq -e '.seq >= 1 and (.row_hash | test(\"^[0-9a-f]{64}$\"))'"
refuse "ossnobody cannot connect to the anchor socket" as ossnobody python3 -I -c '
import socket
s = socket.socket(socket.AF_UNIX); s.connect("/run/ossbot/anchor.sock")'

# Every 10th tick: set the counter to 9, run one tick.
state=/var/lib/ossbot/watchdog.json
before=$(wc -l <"$log")
jq -c '.tick = 9' "$state" >"$state.new" && mv "$state.new" "$state"
watchdog_tick
check "watchdog tick 10 appended an anchor" test "$(wc -l <"$log")" -eq $((before + 1))

check "audit-anchors.log is append-only (lsattr a)" bash -c "lsattr -d '$log' | awk '{print \$1}' | grep -q a"
refuse "truncation refused" truncate -s 0 "$log"
refuse "rewrite refused" bash -c ": >'$log'"
refuse "unlink refused" rm -f "$log"
check "log still there" test -s "$log"
res=$(gk GET /v1/audit/verify)
if [[ $res == 200* && $res == *'"ok":true'* ]]; then pass "audit verify against the anchors ok"; else fail "audit verify: ${res:0:300}"; fi
finish
