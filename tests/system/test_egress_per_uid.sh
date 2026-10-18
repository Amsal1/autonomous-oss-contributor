#!/usr/bin/env bash
# DESIGN §22.6 test_egress_per_uid.sh / §11.4: the uid x port matrix. Each service uid reaches only
# its own proxy port (runner 3140, hermes 3141, gatekeeper 3150), no uid gets direct TCP 443 or
# UDP 53, and through its port only its categories pass (others: CONNECT 403). Root and ossadmin
# are unrestricted; 3128 refuses the host side of the bridge.
# shellcheck source=tests/system/lib.sh disable=SC1091,SC2317
source "$(dirname "$0")/lib.sh"
require_running

PROXY=10.212.0.2
declare -A OWN=([ossrunner]=3140 [hermes]=3141 [gatekeeper]=3150)
tcp() {  # tcp USER HOST PORT -> exit 0 when the connection opens
  as "$1" python3 -I -c 'import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 4).close()' "$2" "$3"
}
udp53() {  # udp53 USER -> exit 0 when a DNS answer comes back from 1.1.1.1 directly
  as "$1" python3 -I -c '
import socket
q = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
s.sendto(q, ("1.1.1.1", 53)); s.recv(512)'
}
dns() {  # dns USER SERVER udp|tcp -> exit 0 when SERVER:53 answers a query (P1-1: resolver on lo)
  as "$1" python3 -I -c '
import socket, sys
q = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
if sys.argv[2] == "udp":
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3); s.sendto(q, (sys.argv[1], 53))
else:
    s = socket.create_connection((sys.argv[1], 53), 3); s.sendall(len(q).to_bytes(2, "big") + q)
assert s.recv(512)' "$2" "$3"
}
mapfile -t RESOLVERS < <(awk '$1 == "nameserver" && $2 ~ /^[0-9.]+$/ {print $2}' /etc/resolv.conf)
RESOLVERS+=(127.0.0.53)
connect() {  # connect USER PORT URL -> prints the CONNECT status code
  as "$1" curl -s -o /dev/null --max-time 20 -w '%{http_connect}' -x "http://$PROXY:$2" "$3" 2>/dev/null || true
}

for u in ossrunner hermes gatekeeper ossbox ossnobody; do
  for p in 3140 3141 3150; do
    if [[ ${OWN[$u]:-} == "$p" ]]; then
      check "$u -> $PROXY:$p (own port) connects" tcp "$u" "$PROXY" "$p"
    else
      refuse "$u -> $PROXY:$p refused" tcp "$u" "$PROXY" "$p"
    fi
  done
  refuse "$u -> 1.1.1.1:443 direct refused" tcp "$u" 1.1.1.1 443
  refuse "$u -> 1.1.1.1:53/udp direct gets no answer" udp53 "$u"
  for ns in "${RESOLVERS[@]}"; do
    for proto in udp tcp; do
      refuse "$u -> resolver $ns:53/$proto gets no answer" dns "$u" "$ns" "$proto"
    done
  done
done
# Control: the resolver itself works (root is unrestricted), so the refusals above are real.
check "root -> resolver ${RESOLVERS[0]}:53/udp answers" dns root "${RESOLVERS[0]}" udp

expect() {  # expect DESCRIPTION WANT GOT
  if [[ $3 == "$2" ]]; then pass "$1 ($3)"; else fail "$1: CONNECT $3, expected $2"; fi
}
expect "gatekeeper via 3150 -> example.com denied" 403 "$(connect gatekeeper 3150 https://example.com/)"
expect "gatekeeper via 3150 -> api.telegram.org allowed" 200 "$(connect gatekeeper 3150 https://api.telegram.org/)"
expect "gatekeeper via 3150 -> api.github.com allowed" 200 "$(connect gatekeeper 3150 https://api.github.com/)"
expect "ossrunner via 3140 -> github.com allowed" 200 "$(connect ossrunner 3140 https://github.com/)"
expect "ossrunner via 3140 -> api.telegram.org denied" 403 "$(connect ossrunner 3140 https://api.telegram.org/)"
expect "ossrunner via 3140 -> gist.github.com denied (always)" 403 "$(connect ossrunner 3140 https://gist.github.com/)"
expect "hermes via 3141 -> example.com allowed (web_public)" 200 "$(connect hermes 3141 https://example.com/)"
expect "hermes via 3141 -> 169.254.169.254 denied (always)" 403 "$(connect hermes 3141 https://169.254.169.254/)"
expect "root via 3128 (host side of the bridge) denied" 403 "$(connect root 3128 https://example.com/)"
check "ossadmin direct 1.1.1.1:443 (unrestricted human)" tcp ossadmin 1.1.1.1 443
check "root direct 1.1.1.1:443 (unrestricted)" tcp root 1.1.1.1 443
finish
