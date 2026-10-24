# shellcheck shell=bash
# The in-container half of the floor-reach test (build-env design §4.6 step 3), fed to `bash -s` of a container
# whose argv is the production one (tests/system/floor_argv.py). test_floor_reach.sh prepends the variables
# SHAPE NETWORKED GW LIVE_GW ETH0 NS BCAST PUB and writes /tmp/floor_probe.py first. Prints key=value lines;
# every decision is made on the host. Nothing here is trusted: it runs as uid 10001 like repo code.
set -u
T=5
say() { printf '%s=%s\n' "$1" "$2"; }

# netshim exactly like the entrypoints start it (only with a network and HTTPS_PROXY)
if [[ -r /opt/ossbot/entrypoints/lib/netshim.sh ]]; then
  # shellcheck source=/dev/null
  source /opt/ossbot/entrypoints/lib/netshim.sh
  ossbot_netshim_start 2>/tmp/netshim.err
fi
if [[ -s /tmp/netshim.pid ]] && kill -0 "$(cat /tmp/netshim.pid)" 2>/dev/null; then say netshim running; else say netshim absent; fi

for p in /var/run/docker.sock /run/docker.sock /run/ossbot; do
  if [[ -e $p ]]; then say "absent_${p//\//_}" no; else say "absent_${p//\//_}" yes; fi
done
# shellcheck disable=SC2012  # a glob listing, no file name parsing
say absent_ossbot_socks "$(ls /run/ossbot/*.sock 2>/dev/null | wc -l | tr -d ' ')"

addr=$(getent hosts example.com | awk '{print $1; exit}')
case $addr in
  127.70.*) say dns_example fake ;;
  "") say dns_example none ;;
  *) say dns_example other ;;
esac

PROXY_URL=${HTTPS_PROXY:-http://proxy:3128}
# through the proxy, explicitly (NO_PROXY unset so loopback literals go to squid too): code/connect/exit
pc() {
  local name=$1 url=$2 out rc
  shift 2
  out=$(env -u NO_PROXY -u no_proxy curl -s -o /dev/null --max-time "$T" -x "$PROXY_URL" -w '%{http_code}/%{http_connect}' "$@" "$url" 2>/dev/null)
  rc=$?
  say "px_$name" "$out/$rc"
}
# through netshim: proxy variables unset, the name resolves to a 127.70 address: exit/code
ns() {
  local out rc
  out=$(env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy curl -s -o /dev/null --max-time "$T" -w '%{http_code}' "$2" 2>/dev/null)
  rc=$?
  say "ns_$1" "$rc/$out"
}
# direct, curl --noproxy '*': exit code
dc() {
  local rc
  curl -s -o /dev/null --noproxy '*' --max-time "$T" "$2" 2>/dev/null
  rc=$?
  say "dc_$1" "$rc"
}
# bash /dev/tcp
bt() {
  # shellcheck disable=SC2016  # $1/$2 expand in the child bash
  if timeout "$T" bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$2" "$3" 2>/dev/null; then say "bt_$1" open; else say "bt_$1" fail; fi
}

if ((NETWORKED)); then
  for n in rfc1918 cgnat meta gw v6 lo pub; do pc "floor_$n" "https://floor-$n.ossbot.test/"; done
  pc literal_10 https://10.0.0.1/
  pc literal_meta http://169.254.169.254/
  pc literal_v6lo 'http://[::1]/'
  pc literal_hex http://0x7f.1/
  pc literal_dec http://2130706433/
  pc literal_pub "https://$PUB/"
  pc uploads https://uploads.github.com/
  pc telegraph https://telegra.ph/
  pc telegram https://api.telegram.org/
  pc kiro https://runtime.us-east-1.kiro.dev/
  pc checkpoint https://checkpoint-api.hashicorp.com/
  pc maven_rebind https://repo.maven.apache.org/
  pc port_literal http://10.0.0.1:8080/
  pc port_tunnel https://192.168.1.1:22/ --proxytunnel
  pc port_name http://floor-rfc1918.ossbot.test:8080/
  ns floor_rfc1918 https://floor-rfc1918.ossbot.test/
  ns telegram https://api.telegram.org/
  ns hex https://0x7f.1/
  ns dec https://2130706433/
  ns floor_gw https://floor-gw.ossbot.test/
fi
for spec in "gw80 http://$GW:80/" "livegw80 http://$LIVE_GW/" "rfc1918 http://10.0.0.1/" "public443 https://1.1.1.1/" \
  "meta http://169.254.169.254/"; do
  dc "${spec%% *}" "${spec#* }"
done
for spec in "gw22 $GW 22" "gw443 $GW 443" "livegw80 $LIVE_GW 80" "eth0_443 $ETH0 443" "public443 1.1.1.1 443"; do
  read -r name host port <<<"$spec"
  bt "$name" "$host" "$port"
done
python3 -I /tmp/floor_probe.py "$GW" "$LIVE_GW" "$ETH0" "$NS" "$BCAST"
