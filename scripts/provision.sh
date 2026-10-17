#!/usr/bin/env bash
# ossbot provisioning (DESIGN §18.2). Runs as root inside the 'ossbot' distro and is streamed
# over stdin from Windows (never executed from the drvfs mount, N4):
#   (Get-Content -Raw scripts/provision.sh) -replace "`r","" | wsl.exe -d ossbot -u root -- bash -s -- [--only a,b] [--list]
# Every step is idempotent and prints 'changed', 'unchanged' or 'skipped (...)'.
# shellcheck disable=SC2317  # step functions are invoked indirectly through OSSBOT_STEPS
set -euo pipefail

OSSBOT_REPO=${OSSBOT_REPO:-/mnt/d/Work/autonomous-oss-contributor}
OSSBOT_LOG_DIR=/srv/ossbot/logs

# Step registry in §18.2 order: "name|function|skip reason (empty = implemented)".
OSSBOT_STEPS=(
  "users|step_users|"
  "apt-base|step_apt_base|"
  "docker|-|not implemented yet (FEAT-004)"
  "dockerfilter|-|not implemented yet (FEAT-004)"
  "uv|step_uv|"
  "app-deploy|-|not implemented yet (FEAT-004)"
  "kiro-cli-hermes|-|phase 2: FEAT-007"
  "hermes|-|phase 2: FEAT-007"
  "images|-|not implemented yet (FEAT-004)"
  "networks|-|not implemented yet (FEAT-004)"
  "firewall|-|not implemented yet (FEAT-004)"
  "systemd|-|not implemented yet (FEAT-004)"
  "audit-anchors|-|not implemented yet (FEAT-004)"
  "secrets-seed|-|not implemented yet (FEAT-004)"
  "enable-start|-|not implemented yet (FEAT-004)"
  "doctor|-|not implemented yet (FEAT-004)"
)

OSSBOT_APT_BASE=(ca-certificates curl git jq unzip rsync iptables acl e2fsprogs shellcheck sudo)

step_apt_base() {
  local pkg missing=()
  for pkg in "${OSSBOT_APT_BASE[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
      missing+=("$pkg")
    fi
  done
  if ((${#missing[@]})); then
    DEBIAN_FRONTEND=noninteractive apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${missing[@]}"
    mark_changed "apt install ${missing[*]}"
  fi

  # gitleaks from its GitHub release, sha256-verified (versions.lock.yaml).
  local want have tmp
  want=$(lock_get gitleaks.version)
  have=$( (/usr/local/bin/gitleaks version 2>/dev/null || true) | tr -d 'v[:space:]')
  if [[ $have != "$want" ]]; then
    tmp=$(mktemp -d)
    download_verified "$(lock_get gitleaks.url)" "$(lock_get gitleaks.sha256)" "$tmp/gitleaks.tar.gz"
    tar -xzf "$tmp/gitleaks.tar.gz" -C "$tmp" gitleaks
    install -o root -g root -m 0755 "$tmp/gitleaks" /usr/local/bin/gitleaks
    rm -rf "$tmp"
    mark_changed "gitleaks ${have:-absent} -> $want"
  fi
}

step_uv() {
  local want have tmp
  want=$(lock_get uv.version)
  have=$( (/opt/ossbot/bin/uv --version 2>/dev/null || true) | awk '{print $2}')
  if [[ $have != "$want" ]]; then
    tmp=$(mktemp -d)
    download_verified "$(lock_get uv.url)" "$(lock_get uv.sha256)" "$tmp/uv.tar.gz"
    tar -xzf "$tmp/uv.tar.gz" -C "$tmp" --strip-components=1
    install -o root -g root -m 0755 "$tmp/uv" /opt/ossbot/bin/uv
    install -o root -g root -m 0755 "$tmp/uvx" /opt/ossbot/bin/uvx
    rm -rf "$tmp"
    mark_changed "uv ${have:-absent} -> $want"
  fi
}

usage() {
  printf 'usage: provision.sh [--only step1,step2] [--list]\n'
}

main() {
  # Everything after this point must not read the script stream on stdin.
  exec </dev/null
  local only="" list=0
  while (($#)); do
    case $1 in
      --only) only=${2:?--only needs a comma-separated step list}; shift 2 ;;
      --only=*) only=${1#--only=}; shift ;;
      --list) list=1; shift ;;
      -h|--help) usage; return 0 ;;
      *) usage >&2; return 2 ;;
    esac
  done

  local entry name fn skip
  if ((list)); then
    for entry in "${OSSBOT_STEPS[@]}"; do
      IFS='|' read -r name fn skip <<<"$entry"
      printf '%s%s\n' "$name" "${skip:+  (skipped: $skip)}"
    done
    return 0
  fi

  [[ $(id -u) == 0 ]] || { printf 'provision.sh must run as root\n' >&2; return 1; }
  [[ -d $OSSBOT_REPO/scripts/lib ]] || { printf 'repo not found at %s\n' "$OSSBOT_REPO" >&2; return 1; }

  # Validate --only names before doing anything.
  local -A known=()
  for entry in "${OSSBOT_STEPS[@]}"; do known[${entry%%|*}]=1; done
  local -a selected=()
  if [[ -n $only ]]; then
    IFS=',' read -r -a selected <<<"$only"
    for name in "${selected[@]}"; do
      [[ -n ${known[$name]:-} ]] || { printf 'unknown step: %s\n' "$name" >&2; return 2; }
    done
  fi

  # Copy the libraries to a root-only temp dir first, so a concurrent edit on the mount
  # cannot change code halfway through a run.
  OSSBOT_LIBDIR=$(mktemp -d)
  trap 'rm -rf "${OSSBOT_LIBDIR:-/nonexistent}"' EXIT
  install -m 0600 "$OSSBOT_REPO"/scripts/lib/common.sh "$OSSBOT_REPO"/scripts/lib/users.sh "$OSSBOT_LIBDIR"/
  # shellcheck source=lib/common.sh disable=SC1091
  source "$OSSBOT_LIBDIR/common.sh"
  # shellcheck source=lib/users.sh disable=SC1091
  source "$OSSBOT_LIBDIR/users.sh"

  install -d -m 0750 "$OSSBOT_LOG_DIR"
  local logfile
  logfile="$OSSBOT_LOG_DIR/provision-$(date -u +%Y%m%dT%H%M%SZ).log"
  exec 3>&1 4>&2
  exec > >(tee -a "$logfile") 2>&1
  local tee_pid=$!
  log "provision start (repo $OSSBOT_REPO, only=${only:-all})"

  local want
  for entry in "${OSSBOT_STEPS[@]}"; do
    IFS='|' read -r name fn skip <<<"$entry"
    if ((${#selected[@]})); then
      want=0
      local s
      for s in "${selected[@]}"; do [[ $s == "$name" ]] && want=1; done
      ((want)) || continue
    fi
    if [[ -n $skip ]]; then
      printf 'step %s: skipped (%s)\n' "$name" "$skip"
      continue
    fi
    OSSBOT_STEP_CHANGED=0
    log "step $name: start"
    "$fn"
    if ((OSSBOT_STEP_CHANGED)); then
      printf 'step %s: changed\n' "$name"
    else
      printf 'step %s: unchanged\n' "$name"
    fi
  done
  log "provision done; log: $logfile"
  # Restore the original stdout/stderr and let tee drain before exiting.
  exec 1>&3 2>&4 3>&- 4>&-
  wait "$tee_pid" 2>/dev/null || true
}

main "$@"; exit $?
