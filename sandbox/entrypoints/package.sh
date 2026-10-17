#!/usr/bin/env bash
# Deterministic package job (DESIGN §9.11 step 3; profile `package`, --network none, no secrets).
# Builds the single commit in a FRESH repository so no repo-controlled git config, hooks, filters or
# fsmonitor of /work/repo ever run, commits on the named branch ossbot-change (M1), and writes
#   /io/output/change.bundle  (exactly refs/heads/ossbot-change, prerequisite ROUND_BASE)
#   /io/output/change.patch   (fixed OSSBOT_FORMAT_PATCH, M7)
#   /io/output/diffstat.json  (fixed OSSBOT_NUMSTAT | numstat2json, R4-N6)
#   /io/output/result.json    ({"ok", "error_class", "head_sha", "round_base"})
#
# Usage: package.sh --round-base <sha> --package-ts <iso8601> --name <name> --email <email>
# OSSBOT_FORMAT_PATCH / OSSBOT_NUMSTAT come from the image environment (ossbot.common.gitcmd).
# OSSBOT_WORK, OSSBOT_IO, OSSBOT_PKG_ROOT and OSSBOT_NUMSTAT2JSON only relocate the fixed paths (tests).
set -euo pipefail

WORK="${OSSBOT_WORK:-/work}"
IO="${OSSBOT_IO:-/io}"
PKG_ROOT="${OSSBOT_PKG_ROOT:-/tmp}"
NUMSTAT2JSON="${OSSBOT_NUMSTAT2JSON:-/opt/ossbot/bin/numstat2json}"
OUT="$IO/output"
REPO="$WORK/repo"
PKG="$PKG_ROOT/pkg"

# sandboxd passes the args as env (the filter binds Cmd to exactly the script, no arguments);
# the command-line flags below override them (tests).
ROUND_BASE="${OSSBOT_ARG_ROUND_BASE:-}"
PACKAGE_TS="${OSSBOT_ARG_PACKAGE_TS:-}"
NAME="${OSSBOT_ARG_NAME:-}"
EMAIL="${OSSBOT_ARG_EMAIL:-}"

result() { # result <ok:true|false> <error_class|null> [head]
  local ok="$1" err="$2" head="${3:-}"
  local err_json="null" head_json="null"
  [[ "$err" != "null" ]] && err_json="\"$err\""
  [[ -n "$head" ]] && head_json="\"$head\""
  printf '{"ok":%s,"error_class":%s,"head_sha":%s,"round_base":"%s"}\n' \
    "$ok" "$err_json" "$head_json" "$ROUND_BASE" >"$OUT/result.json"
}

fail() { # fail <error_class> <message>
  echo "package: $2" >&2
  echo "error_class=$1" >&2
  result false "$1"
  exit 3
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --round-base) ROUND_BASE="${2:-}"; shift 2 ;;
    --package-ts) PACKAGE_TS="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    *) echo "package: unknown argument $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$OUT"
[[ "$ROUND_BASE" =~ ^[0-9a-f]{40}$ ]] || fail bad_args "--round-base must be a 40-hex sha"
[[ "$PACKAGE_TS" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || fail bad_args "--package-ts must be UTC ISO-8601"
[[ -n "$NAME" && "$NAME" != *$'\n'* && "$NAME" != *"<"* && "$NAME" != *">"* ]] || fail bad_args "--name invalid"
[[ "$EMAIL" =~ ^[^@[:space:]\<\>]+@[^@[:space:]\<\>]+$ ]] || fail bad_args "--email invalid"
[[ -n "${OSSBOT_FORMAT_PATCH:-}" && -n "${OSSBOT_NUMSTAT:-}" ]] || fail bad_env "OSSBOT_FORMAT_PATCH/OSSBOT_NUMSTAT unset"
[[ -f "$IO/input/commit_message.txt" ]] || fail bad_args "missing input commit_message.txt"

export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_ATTR_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
export HOME="$PKG_ROOT/pkg-home"
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
mkdir -p "$HOME"
G=(git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.file.allow=never)
read -r -a FORMAT_PATCH <<<"$OSSBOT_FORMAT_PATCH"
read -r -a NUMSTAT <<<"$OSSBOT_NUMSTAT"

rm -rf "$PKG"
"${G[@]}" init -q --object-format=sha1 "$PKG"
cd "$PKG"
# Objects only: the source repository's config is not honoured by a fetch (scoped override, R4-M1).
"${G[@]}" -c protocol.file.allow=always fetch -q --no-tags --no-recurse-submodules "$REPO/.git" "$ROUND_BASE" \
  || fail base_missing "round base $ROUND_BASE not in $REPO"
"${G[@]}" checkout -q -B ossbot-change "$ROUND_BASE"
# --ignore-times: never trust rsync's size+mtime quick check (a same-size edit in the same second
# as the checkout above would otherwise be skipped and silently dropped from the commit).
rsync -a --ignore-times --delete --exclude=/.git "$REPO/" "$PKG/"
"${G[@]}" add -A
if "${G[@]}" diff --cached --quiet; then
  fail empty_change "the working tree equals the round base"
fi
GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL" GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL" \
GIT_AUTHOR_DATE="$PACKAGE_TS" GIT_COMMITTER_DATE="$PACKAGE_TS" \
  "${G[@]}" commit -q --no-verify -F "$IO/input/commit_message.txt" --trailer "Signed-off-by: $NAME <$EMAIL>"
HEAD_SHA="$("${G[@]}" rev-parse refs/heads/ossbot-change)"

"${G[@]}" bundle create -q "$OUT/change.bundle" refs/heads/ossbot-change "^$ROUND_BASE"
"${G[@]}" "${FORMAT_PATCH[@]}" "$ROUND_BASE..refs/heads/ossbot-change" >"$OUT/change.patch"
"${G[@]}" "${NUMSTAT[@]}" "$ROUND_BASE" refs/heads/ossbot-change | "$NUMSTAT2JSON" >"$OUT/diffstat.json"
result true null "$HEAD_SHA"
