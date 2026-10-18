#!/usr/bin/env bash
# No exposure in v1 (DESIGN FR-22, AC 42, ADR-0013; PLAN item 26): the distro has no listener that
# anything outside it could reach. Every TCP/UDP listener in the host netns (`ss -ltnup`) is loopback,
# the WSL resolver on <NS>:53, or a root socket of the WSL VM itself; no container publishes a port
# (the proxy and the sandboxes live on Docker bridges with no port mapping); no tunnel tool is
# installed; the gatekeeper API is a Unix socket only; the Telegram bot long-polls (no webhook).
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
require_running

NS=$(awk '$1 == "nameserver" && $2 ~ /^[0-9.]+$/ {print $2; exit}' /etc/resolv.conf)
read -r eph _ </proc/sys/net/ipv4/ip_local_port_range

# ------------------------------------------------------------------ host listeners
bad=$(ss -H -ltnupe | awk -v ns="$NS" -v eph="$eph" '{
  addr = $5; port = addr; sub(/^.*:/, "", port); host = substr(addr, 1, length(addr) - length(port) - 1)
  gsub(/^\[|\]$/, "", host); sub(/%.*$/, "", host)
  if (host ~ /^127\./ || host == "::1") next                       # loopback
  if (host == ns && port == "53") next                              # the WSL resolver (refused to service uids)
  if ($1 == "udp" && port + 0 >= eph + 0 && $0 !~ /users:\(/ && $0 !~ / uid:/) next   # WSL VM root socket
  print $1 " " addr}')
if [[ -z $bad ]]; then pass "no non-loopback listener in the distro (only lo, $NS:53)"; else fail "non-loopback listeners: $bad"; fi
wild=$(ss -H -ltnu | awk '$5 ~ /^(0\.0\.0\.0|\*|\[::\]|::):/ {print $1 " " $5}')
if [[ -z $wild ]]; then pass "no wildcard (0.0.0.0 / ::) listener"; else fail "wildcard listeners: $wild"; fi

# ------------------------------------------------------------------ containers
published=$(docker ps --format '{{.Names}} {{.Ports}}' | awk '$0 ~ /->/')
if [[ -z $published ]]; then pass "no container publishes a port"; else fail "published ports: $published"; fi
expect_pb() { [[ $(docker inspect -f '{{json .HostConfig.PortBindings}}' "$1") =~ ^(null|\{\})$ ]]; }
for c in $(docker ps -q); do
  name=$(docker inspect -f '{{.Name}}' "$c")
  check "container $name: no PortBindings" expect_pb "$c"
  check "container $name: not on the host network" test "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$c")" != host
done
pid=$(docker inspect -f '{{.State.Pid}}' ossbot-proxy)
read -r peph _ < <(nsenter -t "$pid" -n cat /proc/sys/net/ipv4/ip_local_port_range)
# Besides squid's four TCP ports, the proxy netns holds Docker's embedded resolver (dockerd on
# 127.0.0.11, the container's own loopback) and squid's unconnected UDP DNS-client sockets on
# ephemeral ports (replies to its own queries, not a service).
proxy_bad=$(nsenter -t "$pid" -n ss -H -ltnup | awk -v eph="$peph" '{
  addr = $5; port = addr; sub(/^.*:/, "", port); host = substr(addr, 1, length(addr) - length(port) - 1)
  if (port == "3128" || port == "3140" || port == "3141" || port == "3150") next
  if (host == "127.0.0.11" && $0 ~ /\("dockerd",/) next
  if ($1 == "udp" && $2 == "UNCONN" && port + 0 >= eph + 0 && $0 ~ /\("squid",/) next
  print $1 " " addr}')
if [[ -z $proxy_bad ]]; then
  pass "proxy (own netns, Docker bridges, no published port) listens only on 3128/3140/3141/3150 (+ Docker DNS, squid DNS client)"
else
  fail "proxy listeners beyond the documented ports: $proxy_bad"
fi
for port in 3128 3140 3141 3150; do
  refuse "host netns has no listener on proxy port $port" bash -c "ss -H -ltn | awk '{print \$4}' | grep -Eq ':$port\$'"
done

# ------------------------------------------------------------------ API and tunnels
check "gatekeeper API is a Unix socket" test -S "$GK_SOCK"
gk_uid=$(id -u gatekeeper)
refuse "gatekeeper uid holds no TCP/UDP listener" bash -c "ss -H -ltnue | grep -q ' uid:$gk_uid '"
for tool in cloudflared ngrok tailscale tailscaled frpc localtunnel zrok; do
  refuse "no tunnel tool installed: $tool" bash -c "command -v $tool"
done
check "Telegram bot long-polls (no webhook in the deployed code)" bash -c \
  "grep -q 'start_polling(' /opt/ossbot/app/src/ossbot/gatekeeper/telegram/bot.py && ! grep -rqiE 'set_?webhook|run_webhook|start_webhook' /opt/ossbot/app/src"
finish
