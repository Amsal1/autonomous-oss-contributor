#!/usr/bin/env bash
# Open-discovery design §15 V10/V11, §16 (ADR-0023): the squid `ossbot` log format carries the byte totals
# of a CONNECT tunnel, and ossbot-proxy-rotate loses no line. A container on a fetch-pool test network
# (10.213.63.224/28) downloads from and uploads to pypi.org through proxy:3128; its CONNECT lines must
# show %<st (down) >= the bytes curl received and %>st (up) >= the bytes curl sent. Then the live
# access.log is rotated: the earlier line is in the renamed file, a new request lands in the new file.
# V11b first: with the proxy container not running the rotation is skipped (exit 0, access.log untouched).
# On a failure here, stop and ask the owner before sandbox_open is offered (V10 fallback, design §15).
# Read-only for the mode: it switches no egress mode (the fetch pool reaches pypi in every mode).
# shellcheck source=tests/system/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
require_running

LOG_DIR=/srv/ossbot/logs/proxy
LOG=$LOG_DIR/access.log
IP=10.213.63.230
NET=ossbot-t-logbytes-$$
FULL=$(awk '/^  sandbox_full:/{f=1} f && /digest:/{gsub(/"/,"",$2); print $2; exit}' /etc/ossbot/versions.lock.yaml)
cleanup() {
  docker network disconnect -f "$NET" ossbot-proxy >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

docker network create --internal --subnet 10.213.63.224/28 --label ossbot.sandbox=1 "$NET" >/dev/null ||
  { fail "could not create the test network"; finish; }
docker network connect --alias proxy "$NET" ossbot-proxy

in_pool() {  # in_pool SCRIPT -> output of SCRIPT in a fetch-pool container with the sandbox flags
  docker run --rm --network "$NET" --ip "$IP" --dns 127.0.0.1 --dns-search . --user 10001:10001 \
    --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp:rw,nosuid,nodev,size=16m \
    -e HTTPS_PROXY=http://proxy:3128 "ossbot/sandbox-full@$FULL" bash -c "$1" 2>&1
}
# line_for HOST FROM -> the newest CONNECT line of $IP to HOST:443 in FILE written after line FROM
line_for() {
  local host=$1 from=$2 file=${3:-$LOG}
  tail -n +"$from" "$file" 2>/dev/null | awk -v ip="$IP" -v t="$host:443" '$2 == ip && $5 == "CONNECT" && $6 == t' | tail -n 1
}
wait_line() {  # wait_line HOST FROM [FILE]: tunnels are logged when they close
  local tries=20
  while ((tries-- > 0)); do
    [[ -n $(line_for "$@") ]] && return 0
    sleep 1
  done
  return 1
}

# --------------------------------------------------------------------------- V10: byte totals
start=$(( $(wc -l <"$LOG") + 1 ))
down=$(in_pool 'curl -s -o /dev/null --max-time 60 -w "%{size_download}" https://pypi.org/simple/pip/')
if [[ $down =~ ^[0-9]+$ ]] && ((down > 10000)); then
  pass "download through the fetch pool: $down bytes"
else
  fail "download through the fetch pool failed: '$down'"
fi
if wait_line pypi.org "$start"; then
  read -r _ _ port status method _ _ up_b down_b _ <<<"$(line_for pypi.org "$start")"
  expect_line="port $port, $status $method"
  check "CONNECT line on the sandbox port 3128 ($expect_line)" test "$port" = 3128
  if [[ $down_b =~ ^[0-9]+$ ]] && ((down_b >= down)); then
    pass "CONNECT %<st $down_b >= $down bytes received (V10)"
  else
    fail "CONNECT %<st '$down_b' < $down bytes received: do not offer sandbox_open, ask the owner (V10)"
  fi
  if [[ $up_b =~ ^[0-9]+$ ]] && ((up_b > 0)); then pass "CONNECT %>st $up_b > 0 (V10)"; else fail "CONNECT %>st '$up_b'"; fi
else
  fail "no CONNECT line of $IP to pypi.org:443 within 20 s"
fi

start=$(( $(wc -l <"$LOG") + 1 ))
up=$(in_pool 'head -c 1048576 /dev/urandom >/tmp/blob
  curl -s -o /dev/null --max-time 60 -H "Expect:" --data-binary @/tmp/blob -w "%{size_upload}" https://pypi.org/simple/ || true')
up=${up//[^0-9]/}
if [[ -n $up ]] && ((up >= 65536)); then
  pass "upload through the fetch pool: $up bytes sent"
  if wait_line pypi.org "$start"; then
    read -r _ _ _ _ _ _ _ up_b _ _ <<<"$(line_for pypi.org "$start")"
    if [[ $up_b =~ ^[0-9]+$ ]] && ((up_b >= up)); then
      pass "CONNECT %>st $up_b >= $up bytes sent (V10, the upload signal)"
    else
      fail "CONNECT %>st '$up_b' < $up bytes sent: do not offer sandbox_open, ask the owner (V10)"
    fi
  else
    fail "no CONNECT line for the upload within 20 s"
  fi
else
  fail "the upload probe sent too little to judge ('$up' bytes)"
fi

# ------------------------------------------------------------- V11b: proxy not running
# The boot race (bot-down-after-boot F1): with no running container the script exits 0 and leaves
# access.log alone. An absent container name stands in for a stopped proxy; the live proxy keeps running.
ino=$(stat -c %i "$LOG")
size=$(stat -c %s "$LOG")
newest=$(find "$LOG_DIR" -maxdepth 1 -name 'access.log.2*' -print | sort | tail -n 1)
rc=0
out=$(OSSBOT_PROXY_CONTAINER=ossbot-t-absent-$$ /opt/ossbot/bin/ossbot-proxy-rotate 2>&1) || rc=$?
check "proxy not running: ossbot-proxy-rotate exits 0 (got $rc)" test "$rc" = 0
if [[ $out == *"is not running"* ]]; then pass "proxy not running: the message says so"; else fail "unexpected output: $out"; fi
check "proxy not running: access.log keeps its inode" test "$(stat -c %i "$LOG")" = "$ino"
check "proxy not running: access.log not truncated" test "$(stat -c %s "$LOG")" -ge "$size"
check "proxy not running: no new rotated file" test \
  "$(find "$LOG_DIR" -maxdepth 1 -name 'access.log.2*' -print | sort | tail -n 1)" = "$newest"

# ----------------------------------------------------------------------- V11: rotation
before=$(wc -l <"$LOG")
marker=$(line_for pypi.org 1)
owner=$(stat -c %U:%G "$LOG")
check "ossbot-proxy-rotate runs" /opt/ossbot/bin/ossbot-proxy-rotate
rotated=$(find "$LOG_DIR" -maxdepth 1 -name 'access.log.2*' -print | sort | tail -n 1)  # UTC stamps sort
if [[ -n $rotated && -f $rotated ]]; then
  pass "rotated to $rotated"
  check "rotated file keeps its owner ($owner)" test "$(stat -c %U:%G "$rotated")" = "$owner"
  if (($(wc -l <"$rotated") >= before)); then pass "rotated file holds every earlier line ($before)"; else fail "lines lost in $rotated"; fi
  if grep -qxF -- "$marker" "$rotated"; then pass "the earlier CONNECT line is in the rotated file"; else fail "earlier line missing"; fi
else
  fail "no rotated access.log.<stamp> found"
fi
wait_for 15 test -e "$LOG" || fail "squid did not reopen $LOG after -k rotate"
in_pool 'curl -s -o /dev/null --max-time 30 https://pypi.org/simple/' >/dev/null
if wait_line pypi.org 1 "$LOG"; then pass "a request after the rotation lands in the new access.log"; else fail "new access.log stays empty"; fi
finish
