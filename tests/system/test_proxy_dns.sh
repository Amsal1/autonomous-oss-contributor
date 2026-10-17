#!/usr/bin/env bash
# DESIGN §22.6 test_proxy_dns.sh (M7): the proxy resolves through the WSL resolver; a sandbox on an
# internal task network with --dns 127.0.0.1 cannot resolve anything; the rendered OSSBOT-IN DNS
# accepts name the resolver from /etc/resolv.conf.
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
require_running

check "docker exec ossbot-proxy getent hosts github.com" docker exec ossbot-proxy getent hosts github.com
ns=$(awk '$1 == "nameserver" {print $2; exit}' /etc/resolv.conf)
rules=$(iptables -w -S OSSBOT-IN)
for proto in udp tcp; do
  if grep -q -- "-s 10.212.0.2/32 -d $ns/32 -p $proto -m $proto --dport 53 -j ACCEPT" <<<"$rules"; then
    pass "OSSBOT-IN accepts proxy DNS/$proto to $ns"
  else
    fail "OSSBOT-IN has no $proto 53 accept for $ns"
  fi
done

full=$(awk '/^  sandbox_full:/{f=1} f && /digest:/{gsub(/"/,"",$2); print $2; exit}' /etc/ossbot/versions.lock.yaml)
net=ossbot-t-dnstest-$$
docker network create --internal --subnet 10.213.255.240/28 --label ossbot.sandbox=1 "$net" >/dev/null
docker network connect --alias proxy "$net" ossbot-proxy
out=$(docker run --rm --network "$net" --dns 127.0.0.1 --dns-search . --user 10001:10001 --cap-drop ALL \
  --read-only "ossbot/sandbox-full@$full" sh -c 'getent hosts example.com; echo rc=$?' 2>&1)
if [[ $out == *"rc=2"* || $out == *"rc=1"* ]] && [[ $out != *"93.184"* ]]; then
  pass "a sandbox cannot resolve example.com"
else
  fail "sandbox resolved: $out"
fi
out=$(docker run --rm --network "$net" --dns 127.0.0.1 --user 10001:10001 --cap-drop ALL --read-only \
  -e HTTPS_PROXY=http://proxy:3128 "ossbot/sandbox-full@$full" \
  curl -s -o /dev/null --max-time 15 -w '%{http_connect}' https://example.com/ 2>&1 || true)
# A subnet outside every §10.3 pool gets no squid class: denied, even though the proxy resolves.
if [[ $out == 403 ]]; then pass "unknown-pool sandbox -> proxy:3128 denied (403)"; else fail "unknown-pool sandbox CONNECT '$out'"; fi
docker network disconnect -f "$net" ossbot-proxy >/dev/null 2>&1 || true
docker network rm "$net" >/dev/null
finish
