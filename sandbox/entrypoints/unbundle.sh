#!/usr/bin/env bash
# Unbundle job (DESIGN §9.8 fix rounds, §9.13 step 4 imports; profile `package`, --network none,
# task volume rw). Args are fixed by the runner from gatekeeper state, never from LLM output.
#
# Usage: unbundle.sh --mode fix_round|import --bundle-ref <ref> --checkout-sha <sha> --rev <n>
#   fix_round: input change.bundle, bundle_ref must be refs/heads/ossbot-change (M1, R4-N1);
#              fetch -> refs/ossbot/prev/r<rev>; checkout/reset --hard <checkout_sha = old_head_sha>;
#              git clean -ffd (ignored build caches are kept).
#   import:    input import.bundle, bundle_ref = contributions.import_head_ref; fetch ->
#              refs/ossbot/import; checkout --detach it; reset --soft <checkout_sha = base_sha>.
# list-heads must print exactly "<sha> <bundle_ref>" (else error_class=bundle_refs); a missing
# prerequisite (bundle verify fails) is error_class=bundle_prereq_missing. Only the fetch carries
# the scoped protocol.file.allow=always (R4-M1) and every bundle fetch uses
# --no-recurse-submodules (R5-N1). Writes /io/output/result.json {"ok", "error_class", "head_sha"}.
set -euo pipefail

WORK="${OSSBOT_WORK:-/work}"
IO="${OSSBOT_IO:-/io}"
OUT="$IO/output"
REPO="$WORK/repo"

MODE=""
BUNDLE_REF=""
CHECKOUT_SHA=""
REV=""

result() { # result <ok> <error_class|null> [head]
  local err_json="null" head_json="null"
  [[ "$2" != "null" ]] && err_json="\"$2\""
  [[ -n "${3:-}" ]] && head_json="\"$3\""
  printf '{"ok":%s,"error_class":%s,"head_sha":%s}\n' "$1" "$err_json" "$head_json" >"$OUT/result.json"
}

fail() { # fail <error_class> <message>
  echo "unbundle: $2" >&2
  echo "error_class=$1" >&2
  result false "$1"
  exit 3
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --bundle-ref) BUNDLE_REF="${2:-}"; shift 2 ;;
    --checkout-sha) CHECKOUT_SHA="${2:-}"; shift 2 ;;
    --rev) REV="${2:-}"; shift 2 ;;
    *) echo "unbundle: unknown argument $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$OUT"
case "$MODE" in
  fix_round)
    BUNDLE="$IO/input/change.bundle"
    [[ "$BUNDLE_REF" == "refs/heads/ossbot-change" ]] || fail bundle_refs "fix_round requires refs/heads/ossbot-change"
    [[ "$REV" =~ ^[1-9][0-9]{0,4}$ ]] || fail bad_args "--rev must be a positive integer"
    TARGET="refs/ossbot/prev/r$REV"
    ;;
  import)
    BUNDLE="$IO/input/import.bundle"
    [[ "$BUNDLE_REF" =~ ^refs/heads/[A-Za-z0-9._/-]{1,100}$ ]] || fail bundle_refs "import bundle_ref invalid"
    TARGET="refs/ossbot/import"
    ;;
  *) fail bad_args "--mode must be fix_round or import" ;;
esac
[[ "$CHECKOUT_SHA" =~ ^[0-9a-f]{40}$ ]] || fail bad_args "--checkout-sha must be a 40-hex sha"
[[ -f "$BUNDLE" && ! -L "$BUNDLE" ]] || fail bad_args "missing input bundle"

export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_ATTR_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
export HOME="${OSSBOT_UNBUNDLE_HOME:-/tmp/unbundle-home}"
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
mkdir -p "$HOME"
G=(git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.file.allow=never)
cd "$REPO"

HEADS="$("${G[@]}" bundle list-heads "$BUNDLE")" || fail bundle_refs "git bundle list-heads failed"
if [[ "$(printf '%s\n' "$HEADS" | wc -l)" -ne 1 ]]; then
  fail bundle_refs "expected exactly one head"
fi
HEAD_SHA="${HEADS%% *}"
HEAD_REF="${HEADS#* }"
[[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ && "$HEAD_REF" == "$BUNDLE_REF" ]] || fail bundle_refs "head is not $BUNDLE_REF"
if [[ "$MODE" == "fix_round" && "$HEAD_SHA" != "$CHECKOUT_SHA" ]]; then
  fail bundle_refs "bundle head is not the approved old head"
fi

"${G[@]}" bundle verify -q "$BUNDLE" >/dev/null 2>&1 || fail bundle_prereq_missing "bundle prerequisites missing"
"${G[@]}" -c transfer.fsckObjects=true -c protocol.file.allow=always \
  fetch -q --no-tags --no-recurse-submodules "$BUNDLE" "$BUNDLE_REF:$TARGET" || fail bundle_invalid "bundle fetch failed"
[[ "$("${G[@]}" rev-parse "$TARGET")" == "$HEAD_SHA" ]] || fail bundle_refs "fetched ref differs from list-heads"

if [[ "$MODE" == "fix_round" ]]; then
  # -f: the previous round left its edits uncommitted (reset --soft); reset --hard follows anyway.
  "${G[@]}" checkout -q -f --detach "$CHECKOUT_SHA"
  "${G[@]}" reset -q --hard "$CHECKOUT_SHA"
  "${G[@]}" clean -q -ffd
else
  "${G[@]}" merge-base --is-ancestor "$CHECKOUT_SHA" "$TARGET" || fail bundle_refs "import head does not descend from base"
  "${G[@]}" checkout -q --detach "$TARGET"
  "${G[@]}" reset -q --soft "$CHECKOUT_SHA"
fi
result true null "$HEAD_SHA"
