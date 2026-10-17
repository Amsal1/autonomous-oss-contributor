#!/usr/bin/env bash
# setup-secrets.sh (DESIGN 18.3, ADR-0010, PLAN Decision 5). Runs as root inside the distro and is
# streamed over stdin by scripts/setup-secrets.ps1 together with the data (never written on Windows):
#   <this script> + NUL-separated record  |  wsl.exe -d ossbot -u root --exec bash -s -- --stdin
#   bash -s -- --selftest     validate + write against a temp root only (fake well-formed values)
# Record entries are KEY=VALUE, NUL-terminated. An empty VALUE keeps the existing one. Keys:
#   TELEGRAM_BOT_TOKEN GITHUB_WRITE_TOKEN GITHUB_READ_TOKEN KIRO_API_KEY_SANDBOX KIRO_API_KEY_HERMES
# Files (root:<svc> 0640): gatekeeper.env (GITHUB_WRITE_TOKEN, TELEGRAM_BOT_TOKEN, HERMES_API_KEY,
# OSSBOT_TOKEN_PEPPER, KIRO_KEY_SHA256 = sha256 of both Kiro keys, comma-separated), runner.env
# (GITHUB_READ_TOKEN), sandboxd.env (KIRO_API_KEY), hermes.env (API_SERVER_ENABLED, API_SERVER_KEY =
# HERMES_API_KEY, KIRO_API_KEY). The seeded randoms (OSSBOT_TOKEN_PEPPER, HERMES_API_KEY) are kept.
# Values are never printed. Exit 0 ok, 1 validation error, 2 usage.
set -uo pipefail

SECRETS_DIR=${OSSBOT_SECRETS_DIR:-/etc/ossbot/secrets}
KEYS=(TELEGRAM_BOT_TOKEN GITHUB_WRITE_TOKEN GITHUB_READ_TOKEN KIRO_API_KEY_SANDBOX KIRO_API_KEY_HERMES)
declare -A PATTERN=(
  [TELEGRAM_BOT_TOKEN]='^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$'
  [GITHUB_WRITE_TOKEN]='^ghp_[A-Za-z0-9]{20,}$'
  [GITHUB_READ_TOKEN]='^github_pat_[A-Za-z0-9_]{20,}$'
  [KIRO_API_KEY_SANDBOX]='^ksk_[A-Za-z0-9_-]{16,}$'
  [KIRO_API_KEY_HERMES]='^ksk_[A-Za-z0-9_-]{16,}$'
)
declare -A NEW=()

log() { printf 'setup-secrets: %s\n' "$*"; }

# read_record: NUL-separated KEY=VALUE entries from stdin into NEW; unknown keys are an error.
read_record() {
  local entry key value
  while IFS= read -r -d '' entry || [[ -n ${entry//[$'\r\n ']/} ]]; do
    [[ -n ${entry//[$'\r\n ']/} ]] || continue
    key=${entry%%=*}
    value=${entry#*=}
    if [[ -z ${PATTERN[$key]+x} ]]; then log "unknown key in record: ${key:0:40}"; return 1; fi
    NEW[$key]=$value
    entry=""
  done
  return 0
}

validate() {
  local key bad=0
  for key in "${!NEW[@]}"; do
    [[ -z ${NEW[$key]} ]] && continue
    if [[ ! ${NEW[$key]} =~ ${PATTERN[$key]} ]]; then
      log "invalid format: $key"
      bad=1
    fi
  done
  if [[ -n ${NEW[KIRO_API_KEY_SANDBOX]:-} && ${NEW[KIRO_API_KEY_SANDBOX]:-} == "${NEW[KIRO_API_KEY_HERMES]:-}" ]]; then
    log "the sandbox and the hermes Kiro keys must be two different keys"
    bad=1
  fi
  return "$bad"
}

env_get() { [[ -f $1 ]] && sed -n "s/^$2=//p" "$1" | tail -n 1; return 0; }

# write_env FILE GROUP KEY=VALUE...: rewrite FILE atomically with exactly these keys (empty = drop).
write_env() {
  local file=$1 group=$2 tmp kv; shift 2
  tmp=$(mktemp "$SECRETS_DIR/.tmp-XXXXXX")
  chmod 0600 "$tmp"
  for kv in "$@"; do
    [[ -n ${kv#*=} ]] && printf '%s\n' "$kv" >>"$tmp"
  done
  chown "root:$group" "$tmp"
  chmod 0640 "$tmp"
  mv -f "$tmp" "$file"
}

random_token() { python3 -I -c 'import secrets; print(secrets.token_urlsafe(32))'; }
sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }

apply() {
  local d=$SECRETS_DIR gk=$SECRETS_DIR/gatekeeper.env
  install -d -o root -g root -m 0751 "$d"
  local pepper hkey telegram write read ksb khe hashes=""
  pepper=$(env_get "$gk" OSSBOT_TOKEN_PEPPER); [[ -n $pepper ]] || pepper=$(random_token)
  hkey=$(env_get "$gk" HERMES_API_KEY); [[ -n $hkey ]] || hkey=$(random_token)
  telegram=${NEW[TELEGRAM_BOT_TOKEN]:-$(env_get "$gk" TELEGRAM_BOT_TOKEN)}
  write=${NEW[GITHUB_WRITE_TOKEN]:-$(env_get "$gk" GITHUB_WRITE_TOKEN)}
  read=${NEW[GITHUB_READ_TOKEN]:-$(env_get "$d/runner.env" GITHUB_READ_TOKEN)}
  ksb=${NEW[KIRO_API_KEY_SANDBOX]:-$(env_get "$d/sandboxd.env" KIRO_API_KEY)}
  khe=${NEW[KIRO_API_KEY_HERMES]:-$(env_get "$d/hermes.env" KIRO_API_KEY)}
  [[ -n $ksb ]] && hashes=$(sha "$ksb")
  [[ -n $khe ]] && hashes=${hashes:+$hashes,}$(sha "$khe")
  write_env "$gk" gatekeeper "GITHUB_WRITE_TOKEN=$write" "TELEGRAM_BOT_TOKEN=$telegram" \
    "HERMES_API_KEY=$hkey" "OSSBOT_TOKEN_PEPPER=$pepper" "KIRO_KEY_SHA256=$hashes"
  write_env "$d/runner.env" ossrunner "GITHUB_READ_TOKEN=$read"
  write_env "$d/sandboxd.env" ossbox "KIRO_API_KEY=$ksb"
  write_env "$d/hermes.env" hermes "API_SERVER_ENABLED=true" "API_SERVER_KEY=$hkey" "KIRO_API_KEY=$khe"
  local key set=()
  for key in "${KEYS[@]}"; do [[ -n ${NEW[$key]:-} ]] && set+=("$key"); done
  log "written (${set[*]:-no new values}; seeded randoms preserved)"
}

restart_services() {
  [[ -e /var/lib/ossbot/control/killed ]] && { log "killed: services not restarted"; return 0; }
  local u
  for u in ossbot-gatekeeper.service ossbot-sandboxd.service ossbot-runner.service ossbot-hermes.service; do
    if systemctl is-active --quiet "$u"; then systemctl restart "$u" && log "restarted $u"; fi
  done
}

# shellcheck disable=SC2016  # the check expressions are eval'd later on purpose
selftest() {
  local root fail=0 mode
  root=$(mktemp -d)
  SECRETS_DIR=$root/secrets
  mkdir -p "$SECRETS_DIR"
  printf 'OSSBOT_TOKEN_PEPPER=seeded-pepper\nHERMES_API_KEY=seeded-hermes\n' >"$SECRETS_DIR/gatekeeper.env"
  check() { if ! eval "$2"; then log "selftest FAIL: $1"; fail=1; fi; }
  # Malformed values are refused.
  NEW=([TELEGRAM_BOT_TOKEN]='123:short' [GITHUB_WRITE_TOKEN]='github_pat_wrongkind0000000000000')
  check "malformed values refused" '! validate >/dev/null'
  NEW=([KIRO_API_KEY_SANDBOX]='ksk_same_key_0123456789' [KIRO_API_KEY_HERMES]='ksk_same_key_0123456789')
  check "identical Kiro keys refused" '! validate >/dev/null'
  # A NUL record with fake well-formed values is accepted and written.
  NEW=()
  printf '%s\0' 'TELEGRAM_BOT_TOKEN=123456789:fake_selftest_token_not_real_000' \
    'GITHUB_WRITE_TOKEN=ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKE' 'GITHUB_READ_TOKEN=github_pat_FAKE_FAKE_FAKE_FAKE_FAKE' \
    'KIRO_API_KEY_SANDBOX=ksk_fake_sandbox_0123456789' 'KIRO_API_KEY_HERMES=ksk_fake_hermes_0123456789' \
    >"$root/record"
  read_record <"$root/record" || fail=1
  check "record parsed" '[[ ${#NEW[@]} -eq 5 ]]'
  check "well-formed values accepted" 'validate'
  apply >/dev/null
  for f in gatekeeper:gatekeeper runner:ossrunner sandboxd:ossbox hermes:hermes; do
    mode=$(stat -c '%U:%G %a' "$SECRETS_DIR/${f%%:*}.env")
    check "${f%%:*}.env owner/mode ($mode)" "[[ '$mode' == 'root:${f#*:} 640' ]]"
  done
  check "pepper preserved" '[[ $(env_get "$SECRETS_DIR/gatekeeper.env" OSSBOT_TOKEN_PEPPER) == seeded-pepper ]]'
  check "hermes key preserved" '[[ $(env_get "$SECRETS_DIR/hermes.env" API_SERVER_KEY) == seeded-hermes ]]'
  check "KIRO_KEY_SHA256 lists both keys" \
    '[[ $(env_get "$SECRETS_DIR/gatekeeper.env" KIRO_KEY_SHA256) == "$(sha ksk_fake_sandbox_0123456789),$(sha ksk_fake_hermes_0123456789)" ]]'
  check "runner holds only the read token" '[[ $(cut -d= -f1 "$SECRETS_DIR/runner.env") == GITHUB_READ_TOKEN ]]'
  # Empty values keep what is there.
  NEW=([GITHUB_WRITE_TOKEN]='')
  apply >/dev/null
  check "empty value keeps the existing token" \
    '[[ $(env_get "$SECRETS_DIR/gatekeeper.env" GITHUB_WRITE_TOKEN) == ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKE ]]'
  check "no temp files left" '[[ -z $(find "$SECRETS_DIR" -name ".tmp-*") ]]'
  check "real secrets dir untouched" '[[ $SECRETS_DIR != /etc/ossbot/secrets ]]'
  rm -rf "$root"
  if ((fail)); then log "selftest FAILED"; return 1; fi
  log "selftest passed"
}

main() {
  [[ $EUID -eq 0 ]] || { log "must run as root"; return 2; }
  case ${1:-} in
    --stdin)
      read_record || return 1
      validate || return 1
      apply
      restart_services
      ;;
    --selftest) selftest </dev/null ;;
    *) printf 'usage: setup-secrets.sh --stdin | --selftest\n' >&2; return 2 ;;
  esac
}

# The NUL-separated record follows this line on stdin (setup-secrets.ps1); --stdin reads it.
main "$@"; exit $?
