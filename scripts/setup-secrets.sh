#!/usr/bin/env bash
# setup-secrets.sh (DESIGN 18.3, ADR-0010, PLAN Decision 5). Runs as root inside the distro and is
# streamed over stdin by scripts/setup-secrets.ps1 together with the data (never written on Windows):
#   <this script> + NUL-separated record  |  wsl.exe -d ossbot -u root --exec bash -s -- --stdin
#   bash -s -- --selftest     validate + write against a temp root only (fake well-formed values)
# Record entries are KEY=VALUE, NUL-terminated. An empty VALUE keeps the existing one. Keys:
#   TELEGRAM_BOT_TOKEN GITHUB_WRITE_TOKEN GITHUB_READ_TOKEN KIRO_API_KEY_SANDBOX KIRO_API_KEY_HERMES
#   REVERSE_IMAGE_SEARCH_KEY GITHUB_REVIEW_MIRROR_TOKEN GITHUB_COMMIT_SIGNING_KEY (optional)
# GITHUB_COMMIT_SIGNING_KEY is the one multi-line value (an OpenSSH private-key block; NUL-separated
# records carry newlines): it is checked with ssh-keygen (ed25519, no passphrase) and stored as the
# single-line base64 body of the block.
# Files (root:<svc> 0640): gatekeeper.env (GITHUB_WRITE_TOKEN, TELEGRAM_BOT_TOKEN, HERMES_API_KEY,
# OSSBOT_TOKEN_PEPPER, KIRO_KEY_SHA256 = sha256 of both Kiro keys and the read token, comma-separated,
# REVERSE_IMAGE_SEARCH_KEY, GITHUB_REVIEW_MIRROR_TOKEN, GITHUB_COMMIT_SIGNING_KEY and
# TELEGRAPH_ACCESS_TOKEN when set), runner.env
# (GITHUB_READ_TOKEN), sandboxd.env (KIRO_API_KEY, GITHUB_READ_TOKEN for the MCP jobs), hermes.env
# (API_SERVER_ENABLED, API_SERVER_KEY = HERMES_API_KEY, KIRO_API_KEY, GITHUB_READ_TOKEN). The seeded randoms (OSSBOT_TOKEN_PEPPER, HERMES_API_KEY) are kept.
# Values are never printed. Exit 0 ok, 1 validation error, 2 usage.
set -uo pipefail

SECRETS_DIR=${OSSBOT_SECRETS_DIR:-/etc/ossbot/secrets}
KEYS=(TELEGRAM_BOT_TOKEN GITHUB_WRITE_TOKEN GITHUB_READ_TOKEN KIRO_API_KEY_SANDBOX KIRO_API_KEY_HERMES
  REVERSE_IMAGE_SEARCH_KEY GITHUB_REVIEW_MIRROR_TOKEN GITHUB_COMMIT_SIGNING_KEY)
# GITHUB_COMMIT_SIGNING_KEY: optional (ADR-0006 addendum): the gatekeeper SSH-signs packaged commits
# with it; gatekeeper.env only, never the runner, sandboxd or Hermes.
# GITHUB_REVIEW_MIRROR_TOKEN: optional (ADR-0019 C, off by default): a fine-grained PAT scoped to the one
# private mirror repo; gatekeeper.env only, never Hermes or a sandbox. It must not be the read token.
# REVERSE_IMAGE_SEARCH_KEY: optional hook for the reverse image search feature (DESIGN §14.12.7,
# §14.19 item 11). No provider is wired yet (user decision pending); the key only lands in
# gatekeeper.env, is redacted like every gatekeeper secret, and is never given to Hermes.
declare -A PATTERN=(
  [TELEGRAM_BOT_TOKEN]='^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$'
  [GITHUB_WRITE_TOKEN]='^ghp_[A-Za-z0-9]{20,}$'
  [GITHUB_READ_TOKEN]='^github_pat_[A-Za-z0-9_]{20,}$'
  [KIRO_API_KEY_SANDBOX]='^ksk_[A-Za-z0-9_-]{16,}$'
  [KIRO_API_KEY_HERMES]='^ksk_[A-Za-z0-9_-]{16,}$'
  [REVERSE_IMAGE_SEARCH_KEY]='^[A-Za-z0-9_.:-]{16,256}$'
  [GITHUB_REVIEW_MIRROR_TOKEN]='^github_pat_[A-Za-z0-9_]{20,}$'
  [GITHUB_COMMIT_SIGNING_KEY]='^[A-Za-z0-9+/]{64,8000}={0,2}$'
)
declare -A NEW=()
SIGNING_HEAD='-----BEGIN OPENSSH PRIVATE KEY-----'
SIGNING_TAIL='-----END OPENSSH PRIVATE KEY-----'

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

# signing_key_body: NEW[GITHUB_COMMIT_SIGNING_KEY] (a private-key block, or already its one-line body)
# -> the one-line body, once ssh-keygen confirmed an ed25519 key without a passphrase. The scratch copy
# lives in a 0700 temp dir for the one ssh-keygen call. Nothing about the key is printed.
signing_key_body() {
  local v=${NEW[GITHUB_COMMIT_SIGNING_KEY]} tmp pub rc
  v=${v//$'\r'/}
  if [[ ${v%%$'\n'*} == "$SIGNING_HEAD" ]]; then
    v=${v#"$SIGNING_HEAD"}
    if [[ $v != *"$SIGNING_TAIL"* || -n ${v#*"$SIGNING_TAIL"} && -n ${v#*"$SIGNING_TAIL"$'\n'} ]]; then
      log "invalid format: GITHUB_COMMIT_SIGNING_KEY (not a complete OpenSSH private-key block)"
      return 1
    fi
    v=${v%%"$SIGNING_TAIL"*}
    v=${v//[$'\n' ]/}
  fi
  [[ $v =~ ${PATTERN[GITHUB_COMMIT_SIGNING_KEY]} ]] || { log "invalid format: GITHUB_COMMIT_SIGNING_KEY"; return 1; }
  tmp=$(mktemp -d)
  chmod 0700 "$tmp"
  { printf '%s\n' "$SIGNING_HEAD"; printf '%s\n' "$v" | fold -w 70; printf '%s\n' "$SIGNING_TAIL"; } >"$tmp/key"
  chmod 0600 "$tmp/key"
  pub=$(ssh-keygen -y -P '' -f "$tmp/key" 2>/dev/null)
  rc=$?
  rm -rf "$tmp"
  if ((rc != 0)) || [[ $pub != "ssh-ed25519 "* ]]; then
    log "GITHUB_COMMIT_SIGNING_KEY must be an OpenSSH ed25519 private key without a passphrase"
    return 1
  fi
  NEW[GITHUB_COMMIT_SIGNING_KEY]=$v
}

validate() {
  local key bad=0
  if [[ -n ${NEW[GITHUB_COMMIT_SIGNING_KEY]:-} ]]; then
    signing_key_body || return 1
  fi
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
  if [[ -n ${NEW[GITHUB_REVIEW_MIRROR_TOKEN]:-} && ${NEW[GITHUB_REVIEW_MIRROR_TOKEN]:-} == "${NEW[GITHUB_READ_TOKEN]:-}" ]]; then
    log "the review mirror token must be its own fine-grained token, not the read token"
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
  local pepper hkey telegram write read ksb khe ris mirror signing tgph hashes=""
  pepper=$(env_get "$gk" OSSBOT_TOKEN_PEPPER); [[ -n $pepper ]] || pepper=$(random_token)
  hkey=$(env_get "$gk" HERMES_API_KEY); [[ -n $hkey ]] || hkey=$(random_token)
  telegram=${NEW[TELEGRAM_BOT_TOKEN]:-$(env_get "$gk" TELEGRAM_BOT_TOKEN)}
  write=${NEW[GITHUB_WRITE_TOKEN]:-$(env_get "$gk" GITHUB_WRITE_TOKEN)}
  read=${NEW[GITHUB_READ_TOKEN]:-$(env_get "$d/runner.env" GITHUB_READ_TOKEN)}
  ksb=${NEW[KIRO_API_KEY_SANDBOX]:-$(env_get "$d/sandboxd.env" KIRO_API_KEY)}
  khe=${NEW[KIRO_API_KEY_HERMES]:-$(env_get "$d/hermes.env" KIRO_API_KEY)}
  ris=${NEW[REVERSE_IMAGE_SEARCH_KEY]:-$(env_get "$gk" REVERSE_IMAGE_SEARCH_KEY)}
  mirror=${NEW[GITHUB_REVIEW_MIRROR_TOKEN]:-$(env_get "$gk" GITHUB_REVIEW_MIRROR_TOKEN)}
  signing=${NEW[GITHUB_COMMIT_SIGNING_KEY]:-$(env_get "$gk" GITHUB_COMMIT_SIGNING_KEY)}
  tgph=$(env_get "$gk" TELEGRAPH_ACCESS_TOKEN)  # written by ossctl telegraph setup|rotate, kept here
  [[ -n $ksb ]] && hashes=$(sha "$ksb")
  [[ -n $khe ]] && hashes=${hashes:+$hashes,}$(sha "$khe")
  # GHMCP-2: the read token reaches the MCP jobs and Hermes, so a leak of it is caught like a Kiro key
  [[ -n $read ]] && hashes=${hashes:+$hashes,}$(sha "$read")
  write_env "$gk" gatekeeper "GITHUB_WRITE_TOKEN=$write" "TELEGRAM_BOT_TOKEN=$telegram" \
    "HERMES_API_KEY=$hkey" "OSSBOT_TOKEN_PEPPER=$pepper" "KIRO_KEY_SHA256=$hashes" \
    "REVERSE_IMAGE_SEARCH_KEY=$ris" "GITHUB_REVIEW_MIRROR_TOKEN=$mirror" "GITHUB_COMMIT_SIGNING_KEY=$signing" \
    "TELEGRAPH_ACCESS_TOKEN=$tgph"
  write_env "$d/runner.env" ossrunner "GITHUB_READ_TOKEN=$read"
  # GHMCP-2/4: the read-only GitHub MCP server of the scout/reviewer jobs and of Hermes
  write_env "$d/sandboxd.env" ossbox "KIRO_API_KEY=$ksb" "GITHUB_READ_TOKEN=$read"
  write_env "$d/hermes.env" hermes "API_SERVER_ENABLED=true" "API_SERVER_KEY=$hkey" "KIRO_API_KEY=$khe" \
    "GITHUB_READ_TOKEN=$read"
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

# shellcheck disable=SC2016,SC2034  # the check expressions (and the variables they read) are eval'd on purpose
selftest() {
  local root fail=0 mode body
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
  check "KIRO_KEY_SHA256 lists both keys and the read token" \
    '[[ $(env_get "$SECRETS_DIR/gatekeeper.env" KIRO_KEY_SHA256) == "$(sha ksk_fake_sandbox_0123456789),$(sha ksk_fake_hermes_0123456789),$(sha github_pat_FAKE_FAKE_FAKE_FAKE_FAKE)" ]]'
  check "runner holds only the read token" '[[ $(cut -d= -f1 "$SECRETS_DIR/runner.env") == GITHUB_READ_TOKEN ]]'
  check "sandboxd and hermes get the read token, never the write token" \
    '[[ $(env_get "$SECRETS_DIR/sandboxd.env" GITHUB_READ_TOKEN) == github_pat_FAKE_FAKE_FAKE_FAKE_FAKE && $(env_get "$SECRETS_DIR/hermes.env" GITHUB_READ_TOKEN) == github_pat_FAKE_FAKE_FAKE_FAKE_FAKE && -z $(env_get "$SECRETS_DIR/sandboxd.env" GITHUB_WRITE_TOKEN) && -z $(env_get "$SECRETS_DIR/hermes.env" GITHUB_WRITE_TOKEN) ]]'
  # Empty values keep what is there.
  NEW=([GITHUB_WRITE_TOKEN]='')
  apply >/dev/null
  check "empty value keeps the existing token" \
    '[[ $(env_get "$SECRETS_DIR/gatekeeper.env" GITHUB_WRITE_TOKEN) == ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKE ]]'
  check "no reverse image search key unless given" \
    '[[ -z $(env_get "$SECRETS_DIR/gatekeeper.env" REVERSE_IMAGE_SEARCH_KEY) ]]'
  NEW=([REVERSE_IMAGE_SEARCH_KEY]='fake-reverse-image-key-0123')
  check "reverse image search key accepted" 'validate'
  apply >/dev/null
  NEW=([TELEGRAM_BOT_TOKEN]='')
  apply >/dev/null
  check "reverse image search key kept in gatekeeper.env only" \
    '[[ $(env_get "$SECRETS_DIR/gatekeeper.env" REVERSE_IMAGE_SEARCH_KEY) == fake-reverse-image-key-0123 && -z $(env_get "$SECRETS_DIR/hermes.env" REVERSE_IMAGE_SEARCH_KEY) ]]'
  NEW=([GITHUB_REVIEW_MIRROR_TOKEN]='github_pat_FAKE_FAKE_FAKE_FAKE_FAKE' [GITHUB_READ_TOKEN]='github_pat_FAKE_FAKE_FAKE_FAKE_FAKE')
  check "the read token is refused as the mirror token" '! validate >/dev/null'
  NEW=([GITHUB_REVIEW_MIRROR_TOKEN]='github_pat_FAKE_MIRROR_ONLY_0000000')
  check "mirror token accepted" 'validate'
  apply >/dev/null
  check "mirror token in gatekeeper.env only" \
    '[[ $(env_get "$SECRETS_DIR/gatekeeper.env" GITHUB_REVIEW_MIRROR_TOKEN) == github_pat_FAKE_MIRROR_ONLY_0000000 && -z $(env_get "$SECRETS_DIR/hermes.env" GITHUB_REVIEW_MIRROR_TOKEN) && -z $(env_get "$SECRETS_DIR/sandboxd.env" GITHUB_REVIEW_MIRROR_TOKEN) ]]'
  # SSH commit-signing key (ADR-0006 addendum): a multi-line record value, one line in gatekeeper.env only.
  ssh-keygen -q -t ed25519 -N '' -C '' -f "$root/sign" </dev/null >/dev/null 2>&1
  body=$(sed '1d;$d' "$root/sign" | tr -d '\n')
  NEW=()
  { printf 'GITHUB_COMMIT_SIGNING_KEY='; cat "$root/sign"; printf '\0'; } >"$root/record2"
  read_record <"$root/record2" || fail=1
  check "multi-line signing key read from the record" \
    '[[ ${#NEW[@]} -eq 1 && ${NEW[GITHUB_COMMIT_SIGNING_KEY]} == "$(cat "$root/sign")"* ]]'
  check "signing key accepted" 'validate'
  check "signing key normalised to its one-line body" '[[ ${NEW[GITHUB_COMMIT_SIGNING_KEY]} == "$body" ]]'
  apply >/dev/null
  check "signing key in gatekeeper.env only" \
    '[[ $(env_get "$SECRETS_DIR/gatekeeper.env" GITHUB_COMMIT_SIGNING_KEY) == "$body" && -z $(grep -l GITHUB_COMMIT_SIGNING_KEY "$SECRETS_DIR/runner.env" "$SECRETS_DIR/sandboxd.env" "$SECRETS_DIR/hermes.env") ]]'
  NEW=([TELEGRAM_BOT_TOKEN]='')
  apply >/dev/null
  check "signing key kept when not re-entered" \
    '[[ $(env_get "$SECRETS_DIR/gatekeeper.env" GITHUB_COMMIT_SIGNING_KEY) == "$body" ]]'
  ssh-keygen -q -t ed25519 -N 'selftest passphrase' -C '' -f "$root/locked" </dev/null >/dev/null 2>&1
  NEW=([GITHUB_COMMIT_SIGNING_KEY]="$(cat "$root/locked")")
  check "a passphrase-protected key is refused" '! validate >/dev/null'
  ssh-keygen -q -t rsa -b 2048 -N '' -C '' -f "$root/rsa" </dev/null >/dev/null 2>&1
  NEW=([GITHUB_COMMIT_SIGNING_KEY]="$(cat "$root/rsa")")
  check "a non-ed25519 key is refused" '! validate >/dev/null'
  NEW=([GITHUB_COMMIT_SIGNING_KEY]="$SIGNING_HEAD")
  check "a truncated key block is refused" '! validate >/dev/null'
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
