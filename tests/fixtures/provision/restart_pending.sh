#!/usr/bin/env bash
# Harness for tests/unit/test_provision_restart_pending.py (build-env design §11, F-SN-1): sources the real
# scripts/lib/common.sh and services.sh with the marker dir in a temp dir and stubs for systemctl, _flag_level,
# mark_changed, log, warn and die that append to a calls file (die exits 1).
# usage: restart_pending.sh STATE_DIR CALLS_FILE SCENARIO
#   scenario: app-then-die | install-unit | enable-start | enable-start-app-changed
#   env: FAIL_RESTART=1 makes `systemctl restart` fail
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../../.." && pwd)
export OSSBOT_PROVISION_STATE=$1
CALLS=$2
scenario=$3
OSSBOT_REPO=$repo
# shellcheck source=scripts/lib/common.sh
source "$repo/scripts/lib/common.sh"
# shellcheck source=scripts/lib/services.sh
source "$repo/scripts/lib/services.sh"

log() { printf 'log %s\n' "$*" >>"$CALLS"; }
warn() { printf 'warn %s\n' "$*" >>"$CALLS"; }
die() { printf 'die %s\n' "$*" >>"$CALLS"; exit 1; }
mark_changed() { printf 'changed %s\n' "$*" >>"$CALLS"; }
_flag_level() { echo none; }
systemctl() {
  printf 'systemctl %s\n' "$*" >>"$CALLS"
  if [[ $1 == restart && ${FAIL_RESTART:-0} == 1 ]]; then return 1; fi
  return 0
}

case $scenario in
  app-then-die)  # step app-deploy changed the app, then a later step (images) died
    OSSBOT_APP_CHANGED=1
    restart_pending_mark app
    die "build sandbox-full failed"
    ;;
  install-unit)  # install_unit saw a changed unit file
    ensure_dir() { :; }
    ensure_file() { cat >/dev/null; OSSBOT_STEP_CHANGED=1; }
    install_unit ossbot-gatekeeper.service
    printf 'units_changed %s\n' "$OSSBOT_UNITS_CHANGED" >>"$CALLS"
    ;;
  enable-start)  # a later run in which nothing changed
    step_enable_start
    ;;
  enable-start-app-changed)  # this run changed the app itself
    OSSBOT_APP_CHANGED=1
    restart_pending_mark app
    step_enable_start
    ;;
  *)
    echo "unknown scenario $scenario" >&2
    exit 2
    ;;
esac
