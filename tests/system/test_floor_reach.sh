#!/usr/bin/env bash
# Floor reach (build-env design §4.6, ADR-0025, AC 6), ROOT, after the deploy:
#   wsl.exe -d ossbot -u root --cd /mnt/d/Work/autonomous-oss-contributor --exec bash tests/system/test_floor_reach.sh
# From inside every repo-code shape (fetch, validate, agent-exec, fetch-strict, validate-strict) with the production
# docker-run flags, against test proxies rendered for allowlist, sandbox_open and sandbox_full, every floor must be
# unreachable: the /28 gateway and the distro's services, the WSL/Windows host, RFC1918, CGNAT, link-local and the
# cloud metadata address, loopback names, IPv6, the owner's public address, the always-denied and repo-code-denied
# names, any port other than 80/443, ICMP and raw sockets; directly, through the proxy and through netshim. squid
# marks each floor attempt; the kernel drops squid's own connections to the floors (OSSBOT-FWD, OSSBOT-PUBFLOOR,
# OSSBOT-IN) and ossbot-floorlog writes in/tx lines; a test proxy's lines never hold live work. Step 4b proves the
# fail-closed code-web.txt and that squid re-reads it on SIGHUP (L-11).
# Nothing live changes: no mode switch, the live proxy and its conf are untouched, the test /28s are the
# allocator's reserved tail, containers and networks are created on root's real Docker socket (the filter would
# refuse `bash -s`; the filter's own shapes are tests/unit/dockerfilter), and sandboxd's health and the open holds
# are compared before and after. The owner's public address is only ever printed as <public-ip>.
# shellcheck source=tests/system/lib.sh disable=SC1091,SC2016,SC2317
source "$(dirname "$0")/lib.sh"
require_running

PY=/opt/ossbot/app/.venv/bin/python
SYS=$REPO/tests/system
FLOOR_DIR=/etc/ossbot/proxy/floor
FLOOR_LOG=/srv/ossbot/logs/floor/floor.log
LOCK=/etc/ossbot/versions.lock.yaml
TMP=$(mktemp -d /tmp/ossbot-floor-reach.XXXXXX)
chmod 0755 "$TMP"
TAG=fr$$
image_digest() {  # image_digest KEY -> the pinned digest of images.KEY
  awk -v key="  $1:" '/^images:/ {i=1; next} i && /^[^ #]/ {i=0} i && index($0, key) == 1 {k=1; next}
    i && /^  [a-z_]+:/ {k=0} k && /^    digest:/ {gsub(/"/, "", $2); print $2; exit}' "$LOCK"
}
FULL=ossbot/sandbox-full@$(image_digest sandbox_full)
PROXY_IMG=ossbot/proxy@$(image_digest proxy)
LIVE_GW=$(ip -4 route show default | awk '{print $3; exit}')
ETH0=$(ip -4 -o addr show dev eth0 | awk '{split($4, a, "/"); print a[1]; exit}')
NS=$(awk '$1 == "nameserver" && $2 ~ /^[0-9.]+$/ {print $2; exit}' /etc/resolv.conf)
CONTAINERS=() NETS=() VOLS=()

cleanup() {
  local c n
  for c in "${CONTAINERS[@]}"; do docker rm -f "$c" >/dev/null 2>&1; done
  for n in "${NETS[@]}"; do docker network rm "$n" >/dev/null 2>&1; done
  for c in $(docker volume ls -q --filter "name=$TAG" 2>/dev/null) ossbot-task-c_0000000f7s; do
    docker volume rm "$c" >/dev/null 2>&1
  done
  rm -rf "$TMP"
}
trap cleanup EXIT

expect() { if [[ $3 == "$2" ]]; then pass "$1 ($3)"; else fail "$1: got '$3', expected '$2'"; fi; }
expect_in() {  # expect_in DESCRIPTION GOT ALLOWED...: GOT is one of ALLOWED
  local d=$1 got=$2 a
  shift 2
  for a in "$@"; do [[ $got == "$a" ]] && { pass "$d ($got)"; return; }; done
  fail "$d: got '$got', expected one of: $*"
}

# ------------------------------------------------------------------------------------------------ step 0
[[ $FULL =~ @sha256:[0-9a-f]{64}$ && $PROXY_IMG =~ @sha256:[0-9a-f]{64}$ ]] || { fail "image digests not pinned in $LOCK"; finish; }
[[ -n $LIVE_GW && -n $ETH0 && -n $NS ]] || { fail "live gateway/eth0/resolver not found"; finish; }
# the public-address cases prove nothing without a known public floor: fail, never skip
if ! PUB=$("$PY" "$SYS/floor_argv.py" public); then
  fail "the live public floor is unknown (ossbot-pubip): the public-address cases cannot run"
  finish
fi
pass "the live public floor is known (<public-ip>)"
check "ossbot-floorlog is active" systemctl is-active --quiet ossbot-floorlog.service
check "the floor log exists" test -f "$FLOOR_LOG"
holds_before=$(gk GET '/v1/holds?open=true' | cut -d' ' -f2-)
health() { curl -s --max-time 10 --unix-socket /run/ossbot/sandboxd.sock http://sandboxd/v1/health; }
px_before=$(health | jq -r '.meter.px_other // "missing"')
pending_before=$(/opt/ossbot/app/.venv/bin/ossctl egress holds 2>/dev/null | jq -c '.' 2>/dev/null | md5sum)
T0=$(date +%s.%3N)
SINCE=$(date '+%Y-%m-%d %H:%M:%S')

# ------------------------------------------------------------------------------------- test proxies (step 1)
# start_proxy NAME IP CONF FLOORDIR [--add-host NAME:IP]... : the production flags of ossbot-proxy-run, a test
# conf, a temp log dir, on ossbot-egress at IP; only it resolves the test names (squid's hosts_file).
start_proxy() {
  local name=$1 ip=$2 conf=$3 floor=$4 logs=$TMP/$1-logs
  shift 4
  mkdir -p "$logs" && chown 13:13 "$logs" && chmod 0750 "$logs"
  docker run -d --name "$name" --pull never --network ossbot-egress --ip "$ip" --sysctl net.ipv4.ip_forward=0 \
    --sysctl net.ipv4.tcp_mtu_probing=1 --user 13:13 --read-only --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 512 --memory 1g --memory-swap 1g \
    --tmpfs /run:rw,nosuid,nodev,noexec,mode=1777,size=16m \
    --tmpfs /var/spool/squid:rw,nosuid,nodev,noexec,mode=1777,size=64m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,mode=1777,size=64m \
    --mount "type=bind,src=$conf,dst=/etc/squid/squid.conf,readonly" \
    --mount "type=bind,src=$floor,dst=/etc/squid/floor,readonly" \
    --mount "type=bind,src=$logs,dst=/var/log/squid" \
    --label ossbot.role=floor-test --log-driver local "$@" "$PROXY_IMG" >/dev/null || return 1
  CONTAINERS+=("$name")
  local pid
  pid=$(docker inspect -f '{{.State.Pid}}' "$name")
  wait_for 30 nsenter -t "$pid" -n bash -c '</dev/tcp/127.0.0.1/3128'
}
render_conf() {  # render_conf MODE IP -> the test conf for MODE, host listeners moved to the test proxy's IP
  local out=$TMP/squid-$1.conf
  "$PY" -m ossbot.ctl.render squid --mode "$1" --out "$out" >/dev/null || return 1
  sed -i "s/^http_port 10\.212\.0\.2:/http_port $2:/" "$out"
  chmod 0644 "$out"
  printf '%s' "$out"
}
TEST_HOSTS=(--add-host floor-rfc1918.ossbot.test:10.0.0.1 --add-host floor-cgnat.ossbot.test:100.64.0.1
  --add-host floor-meta.ossbot.test:169.254.169.254 --add-host "floor-gw.ossbot.test:$LIVE_GW"
  --add-host floor-v6.ossbot.test:fd00::1 --add-host floor-lo.ossbot.test:127.0.0.1
  --add-host "floor-pub.ossbot.test:$PUB" --add-host repo.maven.apache.org:10.0.0.1)

# ------------------------------------------------------------------------------------ in-container probe
# run_shape SHAPE NETWORK NET RUN_ID GW BCAST [--add-host ...] -> the probe's key=value output
run_shape() {
  local shape=$1 network=$2 net=$3 run_id=$4 gw=$5 bcast=$6 networked=1
  shift 6
  [[ $network == none ]] && networked=0
  local -a argv
  mapfile -t argv < <("$PY" "$SYS/floor_argv.py" argv "$shape" "$network" "$run_id" "$FULL" "$net" "$@")
  ((${#argv[@]} > 10)) || { echo "argv=failed"; return; }
  VOLS+=("ossbot-in-$run_id" "ossbot-out-$run_id")
  {
    printf 'SHAPE=%q NETWORKED=%q GW=%q LIVE_GW=%q ETH0=%q NS=%q BCAST=%q PUB=%q\n' \
      "$shape" "$networked" "$gw" "$LIVE_GW" "$ETH0" "$NS" "$bcast" "$PUB"
    printf "cat >/tmp/floor_probe.py <<'__OSSBOT_FLOOR_PY__'\n"
    cat "$SYS/floor_probe.py"
    printf '__OSSBOT_FLOOR_PY__\n'
    cat "$SYS/floor_probe.sh"
  } | timeout 180 "${argv[@]}" 2>"$TMP/$run_id.err"
}
val() { sed -n "s/^$2=//p" <<<"$1" | head -n 1; }

# assert_direct DESC OUT NETWORKED: what nothing in a repo-code container may reach directly
assert_direct() {
  local d=$1 out=$2 networked=$3 k v bad=0 n=0
  while IFS='=' read -r k v; do
    case $k in
      gw_tcp_* | gw_udp_* | proxyhost_* | livegw_tcp_* | eth0_tcp_* | ns_tcp_* | direct_* | udp_* | v6_*_443 | v6_mcast*)
        n=$((n + 1))
        [[ $v == open || $v == answer ]] && { fail "$d: $k reached ($v)"; bad=1; } ;;
      icmp_*) n=$((n + 1)); [[ $v == reply ]] && { fail "$d: $k got an echo reply"; bad=1; } ;;
      raw_*) n=$((n + 1)); [[ $v == eperm ]] || { fail "$d: $k not refused with EPERM ($v)"; bad=1; } ;;
      dc_*) n=$((n + 1)); [[ $v == 0 ]] && { fail "$d: curl --noproxy '*' $k connected"; bad=1; } ;;
      bt_*) n=$((n + 1)); [[ $v == open ]] && { fail "$d: bash /dev/tcp $k connected"; bad=1; } ;;
    esac
  done <<<"$out"
  if ((n >= 60 && !bad)); then pass "$d: $n direct, ICMP, raw and IPv6 attempts reached nothing"; elif ((!bad)); then
    fail "$d: only $n direct attempts reported (expected >= 60)"; fi
  expect "$d: no global or ULA IPv6 address" none "$(val "$out" v6_addresses)"
  for p in _var_run_docker.sock _run_docker.sock _run_ossbot; do expect "$d: $p absent" yes "$(val "$out" "absent_$p")"; done
  expect "$d: no /run/ossbot socket" 0 "$(val "$out" absent_ossbot_socks)"
  if ((networked)); then
    expect "$d: netshim running" running "$(val "$out" netshim)"
    expect "$d: glibc answers example.com from netshim's 127.70/16" fake "$(val "$out" dns_example)"
  else
    expect "$d: --network none has only lo" lo "$(val "$out" interfaces)"
    expect "$d: --network none: no resolver" none "$(val "$out" dns_example)"
  fi
}
# proxied: every attempt through the proxy is a 403 (code or CONNECT answer)
assert_proxied() {
  local d=$1 out=$2 k v code conn
  while IFS='=' read -r k v; do
    [[ $k == px_* ]] || continue
    code=${v%%/*} conn=${v#*/} conn=${conn%%/*}
    if [[ $code == 403 || $conn == 403 ]]; then pass "$d: $k denied (403)"; else fail "$d: $k not denied ($v)"; fi
  done <<<"$out"
  for k in ns_floor_rfc1918 ns_telegram ns_hex ns_dec ns_floor_gw; do
    v=$(val "$out" "$k")
    if [[ -n $v && ${v%%/*} != 0 && ${v#*/} != 200 ]]; then pass "$d: $k through netshim got no data ($v)"; else fail "$d: $k through netshim: '$v'"; fi
  done
}
# new_lines LOG FROM PREFIX -> the squid lines after line FROM whose client starts with PREFIX
new_lines() { tail -n "+$(($2 + 1))" "$1" 2>/dev/null | awk -v p="$3" 'index($2, p) == 1'; }
# marks LINES HOST -> the distinct marks of HOST's lines (CONNECT host:port, or an absolute URL's host)
marks() {
  awk -v h="$2" '{u = $6; sub(/^[a-z]+:\/\//, "", u); sub(/\/.*/, "", u); host = u
    if (host ~ /^\[/) sub(/\].*/, "]", host); else sub(/:[0-9]+$/, "", host)
    if (host == h) print $11}' <<<"$1" | sort -u | xargs
}
assert_marks() {  # assert_marks DESC LINES POOL(web|base)
  local d=$1 lines=$2 pool=$3 h want got
  local -A web=([floor-rfc1918.ossbot.test]=dst [floor-cgnat.ossbot.test]=dst [floor-meta.ossbot.test]=dst
    [floor-gw.ossbot.test]=dst [floor-v6.ossbot.test]=dst [floor-pub.ossbot.test]=dst [floor-lo.ossbot.test]=dst_loop
    [repo.maven.apache.org]=dst [10.0.0.1]=literal [192.168.1.1]=literal [169.254.169.254]=literal
    [127.0.0.1]=literal [uploads.github.com]=name [telegra.ph]=name [api.telegram.org]=code
    [runtime.us-east-1.kiro.dev]=code [checkpoint-api.hashicorp.com]=quiet)
  for h in "${!web[@]}"; do
    want=${web[$h]}
    [[ $pool == base && $h == floor-* ]] && want=-  # outside every base category: never resolved, no mark
    got=$(marks "$lines" "$h")
    expect "$d: squid mark of $h" "$want" "$got"
  done
  expect "$d: squid mark of [::1]" literal "$(marks "$lines" '[::1]')"
  got=$(marks "$lines" "$PUB")
  if [[ $got == literal ]]; then pass "$d: squid mark of <public-ip> literal"; else fail "$d: squid mark of <public-ip>: '$got'"; fi
  expect "$d: every squid line is a 403 deny" 0 "$(awk '$4 != "TCP_DENIED/403"' <<<"$lines" | grep -c . || true)"
  expect "$d: netshim sent nothing for the loopback literals (no CONNECT 127.0.0.1:443)" 0 \
    "$(awk '$5 == "CONNECT" && $6 == "127.0.0.1:443"' <<<"$lines" | grep -c . || true)"
}
assert_classes() {  # assert_classes DESC LINES NET: floor_class through `python -m ossbot.common.egress_log classify`
  local d=$1 lines=$2 net=$3 out
  out=$("$PY" -m ossbot.common.egress_log classify --net "$net" --public-file "$FLOOR_DIR/public.txt" <<<"$lines")
  cls() { jq -r --arg h "$1" 'select(.host == $h) | "\(.class)/\(.policy)"' <<<"$out" | sort -u | xargs; }
  expect "$d: class of repo.maven.apache.org" dst/hold "$(cls repo.maven.apache.org)"
  expect "$d: class of 10.0.0.1" literal/hold "$(cls 10.0.0.1)"
  expect "$d: class of 127.0.0.1" loopback/note "$(cls 127.0.0.1)"
  expect "$d: class of uploads.github.com" name/hold "$(cls uploads.github.com)"
  expect "$d: class of checkpoint-api.hashicorp.com" quiet/note "$(cls checkpoint-api.hashicorp.com)"
  if [[ $net == web ]]; then
    expect "$d: class of api.telegram.org" code/hold "$(cls api.telegram.org)"
    expect "$d: class of floor-lo.ossbot.test" loopback/note "$(cls floor-lo.ossbot.test)"
  else
    expect "$d: class of api.telegram.org (base: an ordinary deny there)" code/note "$(cls api.telegram.org)"
  fi
  if [[ $(cls "$PUB") == literal/hold ]]; then pass "$d: class of <public-ip> literal/hold"; else fail "$d: class of <public-ip>: '$(cls "$PUB")'"; fi
}

# --------------------------------------------------------------------- squid-path kernel floor (step 3, host)
# squid_netns NAME: from the test proxy's own netns, NEW connections to the floors time out (dropped in
# OSSBOT-FWD / OSSBOT-PUBFLOOR / OSSBOT-IN); 1.1.1.1:443 connects (the path is open where it should be)
squid_netns() {
  local name=$1 pid t
  pid=$(docker inspect -f '{{.State.Pid}}' "$name")
  local -a targets=("http://$LIVE_GW:80/" "https://$LIVE_GW/" http://192.168.0.1/ http://100.64.0.1/
    http://169.254.169.254/ http://10.0.0.1/ "http://$PUB/" "https://$PUB/" http://10.212.0.1/ "https://$ETH0/"
    http://10.213.63.193/)
  local -A rc=()
  for t in "${targets[@]}"; do
    (nsenter -t "$pid" -n curl -sS --connect-timeout 5 -o /dev/null "$t" 2>/dev/null; echo $? >"$TMP/nsrc-$name-${t//[^a-z0-9]/_}") &
  done
  wait
  for t in "${targets[@]}"; do
    rc[$t]=$(cat "$TMP/nsrc-$name-${t//[^a-z0-9]/_}")
    shown=${t//$PUB/<public-ip>}
    expect "$name netns: $shown times out (kernel floor)" 28 "${rc[$t]}"
  done
  nsenter -t "$pid" -n curl -sS --connect-timeout 5 -k -o /dev/null https://1.1.1.1/ 2>/dev/null
  expect_in "$name netns: positive control https://1.1.1.1/ connects" "$?" 0 60
}

# ----------------------------------------------------------------------------------------- steps 2-4
declare -A IPS=([allowlist]=10.212.0.241 [sandbox_open]=10.212.0.242 [sandbox_full]=10.212.0.243)
NET_A=ossbot-t-$TAG-a NET_X=ossbot-t-c-0000000f7s-exec NET_S=ossbot-t-$TAG-s
docker network rm "$NET_X" >/dev/null 2>&1  # a crashed earlier run's
for mode in allowlist sandbox_open sandbox_full; do
  ip=${IPS[$mode]} proxy=ossbot-proxy-floor-$mode-$$
  conf=$(render_conf "$mode" "$ip") || { fail "$mode: render squid --mode failed"; continue; }
  if ! start_proxy "$proxy" "$ip" "$conf" "$FLOOR_DIR" "${TEST_HOSTS[@]}"; then
    fail "$mode: test proxy did not start"; continue
  fi
  pass "$mode: test proxy $proxy at $ip (production flags, live floor directory read-only)"
  log=$TMP/$proxy-logs/access.log
  docker network create --internal --subnet 10.213.63.192/28 --label ossbot.sandbox=1 "$NET_A" >/dev/null && NETS+=("$NET_A")
  docker network create --internal --subnet 10.213.63.208/28 --label ossbot.sandbox=1 "$NET_X" >/dev/null && NETS+=("$NET_X")
  docker network create --internal --subnet 10.213.223.224/28 --label ossbot.sandbox=1 "$NET_S" >/dev/null && NETS+=("$NET_S")
  for n in "$NET_A" "$NET_X" "$NET_S"; do docker network connect --alias proxy "$n" "$proxy" || fail "$mode: connect $n"; done
  pooltype=base
  [[ $mode == sandbox_open || $mode == sandbox_full ]] && pooltype=web
  for spec in "fetch $NET_A $pooltype 10.213.63.193 10.213.63.207 10.213.63." \
    "validate $NET_A $pooltype 10.213.63.193 10.213.63.207 10.213.63." \
    "agent-exec $NET_X $pooltype 10.213.63.209 10.213.63.223 10.213.63.2" \
    "fetch-strict $NET_S base 10.213.223.225 10.213.223.239 10.213.223.2" \
    "validate-strict $NET_S base 10.213.223.225 10.213.223.239 10.213.223.2"; do
    read -r shape network net gw bcast prefix <<<"$spec"
    if [[ $shape == agent-exec && $mode == allowlist ]]; then network=none; fi  # production: no network (§2.3)
    run_id=$TAG-${shape//-/}-${mode//_/}
    from=$(wc -l <"$log" 2>/dev/null || echo 0)
    out=$(run_shape "$shape" "$network" "$net" "$run_id" "$gw" "$bcast")
    d="$mode/$shape"
    [[ $(val "$out" interfaces) != "" ]] || { fail "$d: the probe produced no output ($(head -c 300 "$TMP/$run_id.err"))"; continue; }
    if [[ $network == none ]]; then
      assert_direct "$d (--network none)" "$out" 0
      continue
    fi
    assert_direct "$d" "$out" 1
    assert_proxied "$d" "$out"
    sleep 1  # squid writes the log line when the transaction ends
    lines=$(new_lines "$log" "$from" "$prefix")
    assert_marks "$d" "$lines" "$net"
    assert_classes "$d" "$lines" "$net"
  done
  # the add-host case: the container resolves floor-gw itself and dials the gateway: no route
  if [[ $mode == sandbox_full ]]; then
    out=$(run_shape validate "$NET_A" web "$TAG-addhost" 10.213.63.193 10.213.63.207 \
      --add-host "floor-gw.ossbot.test:$LIVE_GW")
    v=$(val "$out" ns_floor_gw)
    expect "sandbox_full/validate --add-host floor-gw: direct dial of the live gateway fails (curl exit 7, no route)" 7/000 "$v"
  fi
  expect "$mode: no squid line has a floor server address (consistency)" 0 \
    "$("$PY" "$SYS/floor_argv.py" servers "$log" "$FLOOR_DIR/public.txt")"
  for n in "$NET_A" "$NET_X" "$NET_S"; do docker network disconnect -f "$n" "$proxy" >/dev/null 2>&1; done
  squid_netns "$proxy"
  for n in "$NET_A" "$NET_X" "$NET_S"; do docker network rm "$n" >/dev/null 2>&1; done
  NETS=()
  docker rm -f "$proxy" >/dev/null 2>&1
done

# ----------------------------------------------------------------------------- step 4b: fail closed (L-11)
proxy=ossbot-proxy-floor-closed-$$ ip=10.212.0.244 fdir=$TMP/floor-closed
mkdir -p "$fdir" && cp "$FLOOR_DIR/public.txt" "$fdir/public.txt" && printf '255.255.255.255/32\n' >"$fdir/code-web.txt"
chmod 0755 "$fdir" && chmod 0644 "$fdir"/*.txt
maven=$(getent ahostsv4 repo.maven.apache.org | awk '{print $1; exit}')
example=$(getent ahostsv4 example.com | awk '{print $1; exit}')
conf=$(render_conf sandbox_full "$ip")
if [[ -n $maven && -n $example ]] && start_proxy "$proxy" "$ip" "$conf" "$fdir" \
  --add-host "repo.maven.apache.org:$maven" --add-host "example.com:$example" --add-host "l10.ossbot.test:$PUB"; then
  docker network create --internal --subnet 10.213.63.192/28 --label ossbot.sandbox=1 "$NET_A" >/dev/null && NETS+=("$NET_A")
  docker network connect --alias proxy "$NET_A" "$proxy"
  log=$TMP/$proxy-logs/access.log
  closed_probe() {  # closed_probe RUN_ID -> maven=<connect> example=<connect>
    local -a argv
    mapfile -t argv < <("$PY" "$SYS/floor_argv.py" argv fetch "$NET_A" "$1" "$FULL" web)
    VOLS+=("ossbot-in-$1" "ossbot-out-$1")
    printf '%s\n' "for spec in ${2:-maven=https://repo.maven.apache.org/maven2/ example=https://example.com/}; do
      printf '%s=%s\n' \"\${spec%%=*}\" \"\$(curl -s -o /dev/null --max-time ${3:-15} -w '%{http_connect}' \"\${spec#*=}\")\"; done" |
      timeout 180 "${argv[@]}" 2>/dev/null
  }
  out=$(closed_probe "$TAG-closed1")
  expect "4b: code-web.txt sentinel: a base category still answers (repo.maven.apache.org)" 200 "$(val "$out" maven)"
  expect "4b: code-web.txt sentinel: the public web is denied (example.com)" 403 "$(val "$out" example)"
  expect "4b: that deny is ordinary (no floor mark)" - "$(marks "$(new_lines "$log" 0 10.213.63.)" example.com)"
  printf '10.213.0.0/18\n' >"$fdir/.code-web.new" && chmod 0644 "$fdir/.code-web.new" && mv -f "$fdir/.code-web.new" "$fdir/code-web.txt"
  docker kill --signal=HUP "$proxy" >/dev/null
  sleep 5
  out=$(closed_probe "$TAG-closed2")
  expect "4b/L-11: after the rename + SIGHUP the public web answers (example.com)" 200 "$(val "$out" example)"
  # L-10 (record) and the kernel layer behind squid: with the public address removed from this test proxy's
  # public.txt only, squid lets a name that resolves to it through; OSSBOT-PUBFLOOR still drops the connect
  printf '240.0.0.1/32\n' >"$fdir/.public.new" && chmod 0644 "$fdir/.public.new" && mv -f "$fdir/.public.new" "$fdir/public.txt"
  docker kill --signal=HUP "$proxy" >/dev/null
  sleep 5
  from=$(wc -l <"$log")
  out=$(closed_probe "$TAG-closed3" "l10=https://l10.ossbot.test/" 75)
  expect_in "4b: squid passed a name of the public address it no longer floors, the kernel dropped it (no tunnel)" \
    "$(val "$out" l10)" 000 503 502 504
  l10=$(new_lines "$log" "$from" 10.213.63. | awk '$6 == "l10.ossbot.test:443" {print $4, $7; exit}')
  shown=${l10//$PUB/<public-ip>}
  pass "L-10 (record): squid's line for a CONNECT whose upstream SYN was dropped: '${shown:-no line}'"
else
  fail "4b: fail-closed test proxy not started (maven '$maven', example '$example')"
fi

# --------------------------------------------------------------------------- step 4: the kernel log lines
sleep 3  # journald -> ossbot-floorlog latency
win=$(awk -v t="$T0" '$1 + 0 >= t + 0' "$FLOOR_LOG")
for gw in 10.213.63.193 10.213.63.209 10.213.223.225; do
  expect "floor log: an 'in' TCP line for the gateway $gw" yes \
    "$(awk -v g="$gw" '$2 == "in" && $4 == g && $5 == "TCP" {print "yes"; exit}' <<<"$win")"
  expect "floor log: an 'in' UDP line for the gateway $gw" yes \
    "$(awk -v g="$gw" '$2 == "in" && $4 == g && $5 == "UDP" {print "yes"; exit}' <<<"$win")"
done
for dst in "$LIVE_GW" 192.168.0.1 100.64.0.1 169.254.169.254 10.0.0.1 10.212.0.1 "$ETH0" 10.213.63.193 "$PUB"; do
  shown=${dst//$PUB/<public-ip>}
  expect "floor log: a 'tx' line from a test proxy to $shown" yes \
    "$(awk -v d="$dst" '$2 == "tx" && $3 ~ /^10\.212\.0\.24[1-4]$/ && $4 == d {print "yes"; exit}' <<<"$win")"
done
expect "floor log: no 'px' line in the test's window (only the live proxy's drops hold)" 0 \
  "$(awk '$2 == "px"' <<<"$win" | grep -c . || true)"

# ---------------------------------------------------------------------------- no live hold, no notice
sleep 12  # one meter poll (10 s)
expect "the open holds are unchanged" "$holds_before" "$(gk GET '/v1/holds?open=true' | cut -d' ' -f2-)"
expect "sandboxd's pending hold requests are unchanged" "$pending_before" \
  "$(/opt/ossbot/app/.venv/bin/ossctl egress holds 2>/dev/null | jq -c '.' 2>/dev/null | md5sum)"
px_after=$(health | jq -r '.meter.px_other // "missing"')
if [[ $px_before =~ ^[0-9]+$ && $px_after =~ ^[0-9]+$ ]] && ((px_after > px_before)); then
  pass "the live meter counted the test lines as px_other ($px_before -> $px_after), never as holds"
else
  fail "the live meter's px_other did not grow ('$px_before' -> '$px_after')"
fi
expect "sandboxd: the floor detector is ok" ok "$(health | jq -r '.floor_detector // "missing"')"
expect "sandboxd logged no proxy_floor_unattributed and no floor notice" 0 \
  "$(journalctl -u ossbot-sandboxd --since "$SINCE" -o cat 2>/dev/null | grep -cE 'proxy_floor_unattributed|Floor detector down|floor reach attempt' || true)"
finish
