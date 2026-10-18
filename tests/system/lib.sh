# shellcheck shell=bash
# Shared helpers for tests/system/*.sh (DESIGN §22.6). Run as root on the provisioned distro:
#   wsl.exe -d ossbot -u root --cd /mnt/d/Work/autonomous-oss-contributor --exec bash tests/system/<name>.sh
# Each test exits 0 on success and leaves the system as it found it (running, no flags).
# shellcheck disable=SC2034  # variables are used by the sourcing tests
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

REPO=${OSSBOT_REPO:-/mnt/d/Work/autonomous-oss-contributor}
CONTROL=/var/lib/ossbot/control
GK_SOCK=/run/ossbot/gatekeeper.sock
FILTER_SOCK=/run/ossbot/docker-filter.sock
FAILURES=0
SKIPS=0
TEST_NAME=$(basename "${BASH_SOURCE[1]:-$0}" .sh)

[[ $EUID -eq 0 ]] || { echo "$TEST_NAME: run as root" >&2; exit 2; }

pass() { printf 'PASS %s: %s\n' "$TEST_NAME" "$*"; }
fail() { printf 'FAIL %s: %s\n' "$TEST_NAME" "$*"; FAILURES=$((FAILURES + 1)); }
# skip DESCRIPTION: a check that did not run (counted apart from PASS, reported by finish)
skip() { printf 'SKIP %s: %s\n' "$TEST_NAME" "$*"; SKIPS=$((SKIPS + 1)); }
# check DESCRIPTION CMD...: pass when CMD succeeds
check() { local d=$1; shift; if "$@" >/dev/null 2>&1; then pass "$d"; else fail "$d"; fi; }
# refuse DESCRIPTION CMD...: pass when CMD fails
refuse() { local d=$1; shift; if "$@" >/dev/null 2>&1; then fail "$d (unexpectedly succeeded)"; else pass "$d"; fi; }
as() { local u=$1; shift; runuser -u "$u" -- "$@"; }

# wait_for SECONDS CMD...: poll until CMD succeeds
wait_for() {
  local secs=$1 i; shift
  for ((i = 0; i < secs * 2; i++)); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  return 1
}

# gk METHOD PATH [JSON] [USER]: print "<status> <body>" of a gatekeeper API call.
gk() {
  local method=$1 path=$2 body=${3:-} user=${4:-root} out
  local -a argv=(curl -s -o - -w '\n%{http_code}' --max-time 30 --unix-socket "$GK_SOCK" -X "$method")
  [[ -n $body ]] && argv+=(-H 'content-type: application/json' --data "$body")
  out=$(as "$user" "${argv[@]}" "http://ossbot$path" 2>/dev/null)
  printf '%s %s\n' "$(tail -n 1 <<<"$out")" "$(head -n -1 <<<"$out" | tr -d '\n')"
}

flag_reason() { jq -r '.reason // empty' "$CONTROL/killed" 2>/dev/null; }

units_active() {
  local u
  for u in "$@"; do systemctl is-active --quiet "$u" || return 1; done
}

PHASE1_UNITS=(ossbot-boot-gate ossbot-firewall ossbot-firewall-docker ossbot-dockerfilter ossbot-gatekeeper
  ossbot-proxy ossbot-sandboxd ossbot-watchdog.timer)

# resume_all: offline resume (the root helper) and wait until every Phase 1 unit is active again.
resume_all() {
  /opt/ossbot/bin/ossbot-resume --offline --via "test:$TEST_NAME" --actor test >/dev/null 2>&1
  wait_for 90 units_active "${PHASE1_UNITS[@]}"
}

require_running() {
  if [[ -e $CONTROL/killed || -e $CONTROL/paused ]]; then
    echo "$TEST_NAME: system is killed or paused before the test; resume first" >&2
    exit 2
  fi
  wait_for 60 units_active "${PHASE1_UNITS[@]}" || { echo "$TEST_NAME: ossbot units not active" >&2; exit 2; }
}

# The watchdog tick, run now instead of waiting for the timer.
watchdog_tick() { systemctl start ossbot-watchdog.service; }

finish() {
  if ((FAILURES)); then
    printf '%s: %d check(s) FAILED\n' "$TEST_NAME" "$FAILURES"
    exit 1
  fi
  if ((SKIPS)); then
    printf '%s: all checks passed, %d SKIPPED (not run)\n' "$TEST_NAME" "$SKIPS"
  else
    printf '%s: all checks passed\n' "$TEST_NAME"
  fi
  exit 0
}
