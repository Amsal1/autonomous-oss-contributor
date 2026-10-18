#!/usr/bin/env bash
# Egress threat table (docs/VERIFICATION.md "Egress threat table"; ADR-0005 addendum for Phase 1
# review P1-11/P1-12). One probe per row that the other system tests do not already cover:
#   squid  no DNS for a name or an IP literal the caller may not reach, per host port and per
#          sandbox pool (a raw-table string match counts the proxy's queries for a fresh random
#          label and every PTR query), IP literals denied on every port, names resolving to a
#          denied range refused, the rendered config uses -n on every name ACL
#   ICMP / raw / IPv6 / direct IP from sandboxes and service uids; the host's listeners;
#   the proxy netns; the Hyper-V layer (root's non-80/443 egress), SKIP when not applied.
# shellcheck source=tests/system/lib.sh disable=SC1091,SC2317,SC2016
source "$(dirname "$0")/lib.sh"
require_running

PROXY=10.212.0.2
RAW=OSSBOT-TDNS-$$
NS=$(awk '$1 == "nameserver" && $2 ~ /^[0-9.]+$/ {print $2; exit}' /etc/resolv.conf)
FULL=$(awk '/^  sandbox_full:/{f=1} f && /digest:/{gsub(/"/,"",$2); print $2; exit}' /etc/ossbot/versions.lock.yaml)
declare -A POOL_NET=([fetch]=10.213.63.240/28 [research]=10.213.127.240/28 [llm]=10.213.191.240/28)
NETS=()

cleanup() {
  local proto net
  for proto in udp tcp; do
    while iptables -w -t raw -D PREROUTING -s "$PROXY/32" -p "$proto" --dport 53 -j "$RAW" 2>/dev/null; do :; done
  done
  iptables -w -t raw -F "$RAW" 2>/dev/null
  iptables -w -t raw -X "$RAW" 2>/dev/null
  for net in "${NETS[@]}"; do
    docker network disconnect -f "$net" ossbot-proxy >/dev/null 2>&1
    docker network rm "$net" >/dev/null 2>&1
  done
}
trap cleanup EXIT

# --------------------------------------------------------------------- proxy DNS meter
# The proxy's queries to the WSL resolver arrive over the bridge, so raw PREROUTING sees each one
# before OSSBOT-IN accepts it. The chain only counts (RETURN); OSSBOT-* filter chains are untouched.
iptables -w -t raw -N "$RAW"
for proto in udp tcp; do
  iptables -w -t raw -I PREROUTING 1 -s "$PROXY/32" -p "$proto" --dport 53 -j "$RAW"
done
iptables -w -t raw -A "$RAW" -m string --algo bm --hex-string '|07|in-addr|04|arpa' -m comment --comment ptr4 -j RETURN
iptables -w -t raw -A "$RAW" -m string --algo bm --hex-string '|03|ip6|04|arpa' -m comment --comment ptr6 -j RETURN
label() {  # label NAME -> a fresh random DNS label, counted under comment NAME
  local l
  l="osbt$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
  iptables -w -t raw -A "$RAW" -m string --algo bm --string "$l" -m comment --comment "$1" -j RETURN
  printf '%s' "$l"
}
count() {  # count NAME -> packets the proxy sent to its resolver matching NAME's rule
  iptables -w -t raw -L "$RAW" -v -n -x | awk -v c="/* $1 */" 'index($0, c) {print $1; found=1} END {if (!found) print "missing"}'
}
connect() {  # connect USER PORT URL -> the CONNECT status code
  as "$1" curl -s -o /dev/null --max-time 20 -w '%{http_connect}' -x "http://$PROXY:$2" "$3" 2>/dev/null || true
}
expect() {  # expect DESCRIPTION WANT GOT
  if [[ $3 == "$2" ]]; then pass "$1 ($3)"; else fail "$1: got '$3', expected $2"; fi
}
expect_not() {  # expect_not DESCRIPTION UNWANTED GOT
  if [[ $3 != "$2" ]]; then pass "$1 ($3)"; else fail "$1: got '$3'"; fi
}

# ------------------------------------------------------------ host ports (P1-11, P1-12)
declare -A HOST_LABEL=()
for spec in ossrunner:3140 gatekeeper:3150 root:3128; do
  u=${spec%%:*} p=${spec##*:}
  HOST_LABEL[$spec]=$(label "host-$p")
  expect "$u via $p -> random label denied" 403 "$(connect "$u" "$p" "https://${HOST_LABEL[$spec]}.example.com/")"
  expect "$u via $p -> IPv4 literal denied" 403 "$(connect "$u" "$p" "https://203.0.113.$((p % 200))/")"
  expect "$u via $p -> IPv6 literal denied" 403 "$(connect "$u" "$p" "https://[2001:db8::$p]/")"
done
expect "hermes via 3141 -> IPv4 literal denied (ip_literal, even with web_public)" 403 \
  "$(connect hermes 3141 https://203.0.113.141/)"
expect "hermes via 3141 -> decimal IPv4 literal denied" 403 "$(connect hermes 3141 https://3405803917/)"
expect "hermes via 3141 -> IPv6 literal denied" 403 "$(connect hermes 3141 'https://[2001:db8::3141]/')"
# Control: hermes has web_public, so squid does resolve its names. The meter must see that query.
control=$(label control)
expect_not "hermes via 3141 -> random label not allowed (NXDOMAIN)" 200 "$(connect hermes 3141 "https://$control.example.com/")"
# squid normalizes the host (case, trailing dot) before the ACLs, so a denied name cannot be
# spelled past denied_domains; raw CONNECT lines, so no client rewrites them. Non-443 CONNECT denied.
raw_connect() {  # raw_connect USER PORT TARGET -> status line of a hand-written CONNECT
  as "$1" timeout 15 bash -c 'exec 3<>"/dev/tcp/$1/$2"; printf "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" "$3" "$3" >&3
    head -n 1 <&3 | cut -d" " -f2' _ "$PROXY" "$2" "$3" 2>/dev/null || true
}
for target in gist.github.com.:443 GIST.GitHub.COM:443 x.github.io.:443 example.com:8443 example.com:22; do
  expect "hermes via 3141 -> raw CONNECT $target denied" 403 "$(raw_connect hermes 3141 "$target")"
done
# The resolved-IP deny still applies to allowed names: localtest.me resolves to 127.0.0.1.
if getent ahostsv4 localtest.me 2>/dev/null | grep -q '^127\.'; then
  expect "hermes via 3141 -> localtest.me (resolves to 127.0.0.1) denied by denied_dst" 403 \
    "$(connect hermes 3141 https://localtest.me/)"
else
  pass "SKIP localtest.me does not resolve to 127.0.0.1 here (resolved-IP deny probe not run)"
fi

# ----------------------------------------------------------------- sandbox pools (3128)
# One container per §10.3 pool on a test network in the last /28 of the pool, with the sandbox
# flags of sandboxd's templates. squid classes by source subnet only.
POOL_SCRIPT='
connect() { curl -s -o /dev/null --max-time 15 -w "%{http_connect}" -x http://proxy:3128 "$1" 2>/dev/null || true; }
echo "label=$(connect "https://$LABEL.example.com/")"
echo "ipv4=$(connect "https://203.0.113.$OCTET/")"
echo "ipv6=$(connect "https://[2001:db8::$OCTET]/")"
echo "control=$(connect "$CONTROL_URL")"
python3 -I - <<"PY"
import socket
def res(fn):
    try:
        return fn()
    except socket.timeout:
        return "timeout"
    except OSError as exc:
        return f"errno{exc.errno}"
def tcp(host, port, fam=socket.AF_INET):
    def go():
        s = socket.socket(fam, socket.SOCK_STREAM); s.settimeout(4); s.connect((host, port)); s.close(); return "open"
    return res(go)
def icmp(host):
    def go():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP); s.settimeout(3)
        s.sendto(b"\x08\x00\x00\x00\x00\x01\x00\x01ossbot", (host, 0)); s.recv(64); return "reply"
    return res(go)
def raw():
    def go():
        socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP).close(); return "open"
    return res(go)
def udp53(host):
    def go():
        q = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3); s.sendto(q, (host, 53)); s.recv(512); return "answer"
    return res(go)
v6 = [l.split()[-1] for l in open("/proc/net/if_inet6")] if __import__("os").path.exists("/proc/net/if_inet6") else []
gw = open("/proc/net/route").read().split("\n")[1:]
print("direct443=" + tcp("1.1.1.1", 443))
print("icmp=" + icmp("1.1.1.1"))
print("raw=" + raw())
print("resolver53=" + udp53("10.255.255.254"))
print("v6ifaces=" + (",".join(sorted(set(v6) - {"lo"})) or "none"))
print("v6direct=" + tcp("2606:4700:4700::1111", 443, socket.AF_INET6))
print("defaultroute=" + ("yes" if any(l.split()[1:2] == ["00000000"] for l in gw if l.strip()) else "no"))
print("hostports=" + ",".join(tcp("10.212.0.2", p) for p in (3140, 3141, 3150)))
PY
'
declare -A POOL_LABEL=() POOL_OUT=()
declare -A CONTROL_URL=([fetch]=https://pypi.org/ [research]=https://example.com/ [llm]=https://app.kiro.dev/)
octet=10
for pool in fetch research llm; do
  net="ossbot-t-egresstest-$pool-$$"
  docker network create --internal --subnet "${POOL_NET[$pool]}" --label ossbot.sandbox=1 "$net" >/dev/null || {
    fail "$pool: could not create test network ${POOL_NET[$pool]}"; continue; }
  NETS+=("$net")
  docker network connect --alias proxy "$net" ossbot-proxy
  POOL_LABEL[$pool]=$(label "pool-$pool")
  octet=$((octet + 1))
  POOL_OUT[$pool]=$(docker run --rm --network "$net" --dns 127.0.0.1 --dns-search . --user 10001:10001 \
    --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp:rw,nosuid,nodev,size=16m \
    -e "LABEL=${POOL_LABEL[$pool]}" -e "OCTET=$octet" -e "CONTROL_URL=${CONTROL_URL[$pool]}" \
    "ossbot/sandbox-full@$FULL" bash -c "$POOL_SCRIPT" 2>&1)
done
field() { sed -n "s/^$2=//p" <<<"${POOL_OUT[$1]:-}" | head -n 1; }
for pool in fetch research llm; do
  [[ -n ${POOL_OUT[$pool]:-} ]] || continue
  if [[ $pool == research ]]; then
    expect_not "pool $pool -> random label not allowed (web_public resolves; NXDOMAIN)" 200 "$(field "$pool" label)"
  else
    expect "pool $pool -> random label denied" 403 "$(field "$pool" label)"
  fi
  expect "pool $pool -> IPv4 literal denied" 403 "$(field "$pool" ipv4)"
  expect "pool $pool -> IPv6 literal denied" 403 "$(field "$pool" ipv6)"
  expect "pool $pool -> its own category still allowed (${CONTROL_URL[$pool]})" 200 "$(field "$pool" control)"
  expect_not "pool $pool -> direct 1.1.1.1:443 fails" open "$(field "$pool" direct443)"
  expect_not "pool $pool -> ICMP echo to 1.1.1.1 gets no reply" reply "$(field "$pool" icmp)"
  expect_not "pool $pool -> raw socket refused (no CAP_NET_RAW)" open "$(field "$pool" raw)"
  expect_not "pool $pool -> WSL resolver $NS:53 gets no answer" answer "$(field "$pool" resolver53)"
  expect "pool $pool -> no IPv6 address on any interface" none "$(field "$pool" v6ifaces)"
  expect_not "pool $pool -> IPv6 direct fails" open "$(field "$pool" v6direct)"
  expect "pool $pool -> no default route (internal network)" no "$(field "$pool" defaultroute)"
  hp=$(field "$pool" hostports)
  if [[ -n $hp && $hp != *open* ]]; then pass "pool $pool -> 10.212.0.2:3140/3141/3150 unreachable ($hp)"; else fail "pool $pool -> host proxy ports: '$hp'"; fi
done

# ------------------------------------------------- the meter: no lookup for denied requests
sleep 2  # let squid finish any lookup it might (wrongly) have started
for spec in ossrunner:3140 gatekeeper:3150 root:3128; do
  expect "proxy sent no DNS query for the label denied on ${spec##*:} (P1-12)" 0 "$(count "host-${spec##*:}")"
done
for pool in fetch llm; do
  [[ -n ${POOL_LABEL[$pool]:-} ]] && expect "proxy sent no DNS query for the label denied in pool $pool (P1-12)" 0 "$(count "pool-$pool")"
done
expect "proxy sent no PTR query for any IP literal, IPv4 (P1-11)" 0 "$(count ptr4)"
expect "proxy sent no PTR query for any IP literal, IPv6 (P1-11)" 0 "$(count ptr6)"
c=$(count control)
if [[ $c =~ ^[0-9]+$ ]] && ((c > 0)); then
  pass "meter control: hermes' web_public lookup was counted ($c packets), so zero counts are real"
else
  fail "meter control: hermes' web_public lookup not counted ('$c'); the zero counts prove nothing"
fi

# ---------------------------------------------------------- squid config and proxy netns
conf=/etc/ossbot/proxy/squid.conf
bad=$(awk '$1 == "acl" && ($3 == "dstdomain" || $3 == "dstdom_regex") && $4 != "-n" {print $2}' "$conf")
if [[ -z $bad ]]; then pass "live squid.conf: every name ACL is -n"; else fail "live squid.conf: name ACLs without -n: $bad"; fi
if grep -Eq '^http_access deny .*denied_dst' "$conf"; then fail "live squid.conf: a deny line resolves (denied_dst)"; else pass "live squid.conf: no deny line uses the dst ACL"; fi
check "live squid.conf: pinger off" grep -qx 'pinger_enable off' "$conf"
refuse "live squid.conf: no TLS interception (ssl_bump / https_port / sslcrtd)" grep -Eq '^(ssl_bump|https_port|sslcrtd_program)' "$conf"
check "live squid.conf: visible_hostname set (no hostname lookup at start)" grep -Eq '^visible_hostname ' "$conf"
check "live squid.conf: dns_nameservers is the resolv.conf resolver $NS" grep -qx "dns_nameservers $NS" "$conf"
pid=$(docker inspect -f '{{.State.Pid}}' ossbot-proxy)
expect "proxy netns: ip_forward" 0 "$(nsenter -t "$pid" -n sysctl -n net.ipv4.ip_forward)"
expect "proxy netns: IPv6 only on lo" "lo" "$(nsenter -t "$pid" -n awk '{print $6}' /proc/net/if_inet6 | sort -u | tr '\n' ' ' | xargs)"
expect "proxy: no capabilities (no raw sockets, pinger inert)" 0000000000000000 \
  "$(awk '/^CapEff/ {print $2}' "/proc/$pid/status")"

# ------------------------------------------------------------------ service uids: ICMP, IPv6
icmp_uid() {  # icmp_uid USER -> exit 0 when an ICMP echo to 1.1.1.1 gets a reply
  as "$1" python3 -I -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP); s.settimeout(3)
s.sendto(b"\x08\x00\x00\x00\x00\x01\x00\x01ossbot", ("1.1.1.1", 0)); s.recv(64)'
}
tcp6() {  # tcp6 USER HOST PORT -> exit 0 when the IPv6 connection opens
  as "$1" python3 -I -c '
import socket, sys
s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM); s.settimeout(4); s.connect((sys.argv[1], int(sys.argv[2])))' "$2" "$3"
}
for u in ossrunner hermes gatekeeper ossbox ossnobody; do
  refuse "$u -> ICMP echo 1.1.1.1 gets no reply" icmp_uid "$u"
  refuse "$u -> IPv6 [2606:4700:4700::1111]:443 refused" tcp6 "$u" 2606:4700:4700::1111 443
done
check "ip6tables INPUT jumps to OSSBOT-IN first" test "$(ip6tables -w -S INPUT | sed -n 2p)" = "-A INPUT -j OSSBOT-IN"
refuse "host has no global IPv6 route" bash -c 'ip -6 route show default | grep -q .'

# --------------------------------------------------------------------------- host surface
# A service on a wildcard or bridge address would be reachable from the bridges but for OSSBOT-IN
# (IPv4 by source, IPv6 by interface). Defense in depth: none exists. UDP sockets on ephemeral
# ports are client sockets (systemd-timesyncd's NTP query binds 0.0.0.0:<ephemeral> while syncing).
read -r eph _ </proc/sys/net/ipv4/ip_local_port_range
bad=$(ss -H -ltnu | awk -v ns="$NS" -v eph="$eph" '{a=$5; port=a; sub(/^.*:/, "", port)
  sub(/:[^:]*$/, "", a); gsub(/[][]/, "", a); sub(/%.*/, "", a)
  if ($1 == "udp" && port + 0 >= eph + 0) next
  if (a !~ /^127\./ && a != "::1" && a != ns) print $1 " " $5}')
if [[ -z $bad ]]; then pass "host listeners: only loopback and the WSL resolver"; else fail "host listeners on non-loopback addresses: $bad"; fi
check "nsswitch hosts: files dns only" grep -Eq '^hosts:[[:space:]]+files dns[[:space:]]*$' /etc/nsswitch.conf
for p in nscd unscd sssd dnsmasq unbound bind9 avahi-daemon libnss-resolve libnss-mdns; do
  refuse "no caching/forwarding resolver or nss DNS module: $p" bash -c "dpkg-query -W -f='\${Status}' $p 2>/dev/null | grep -q 'install ok installed'"
done

# ---------------------------------------------------------------- Hyper-V layer (root)
tcp_root() { timeout 8 python3 -I -c 'import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 5).close()' "$1" "$2"; }
udp53_root() {
  timeout 8 python3 -I -c '
import socket
q = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4); s.sendto(q, ("1.1.1.1", 53)); s.recv(512)'
}
icmp_root() { ping -c 2 -W 2 1.1.1.1; }
open=0
tcp_root 1.1.1.1 853 >/dev/null 2>&1 && open=$((open + 1))
udp53_root >/dev/null 2>&1 && open=$((open + 1))
icmp_root >/dev/null 2>&1 && open=$((open + 1))
if ((open == 3)); then
  pass "SKIP Hyper-V firewall not applied (optional layer, DESIGN 7.8): root reaches 1.1.1.1 on 853/tcp, 53/udp, ICMP"
else
  refuse "Hyper-V: root -> 1.1.1.1:853/tcp blocked" tcp_root 1.1.1.1 853
  refuse "Hyper-V: root -> 1.1.1.1:53/udp blocked" udp53_root
  refuse "Hyper-V: root -> ICMP 1.1.1.1 blocked" icmp_root
fi
check "Hyper-V control: root -> 1.1.1.1:443/tcp allowed" tcp_root 1.1.1.1 443
check "root still resolves through the WSL resolver (DNS tunnel, not the VM NIC)" getent hosts github.com
finish
