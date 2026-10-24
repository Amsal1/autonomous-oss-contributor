#!/usr/bin/env bash
# Validate job (DESIGN §9.9; sandbox-full, profile validate, registries via the proxy, no secrets,
# task volume rw). Runs the runner-composed plan /io/input/<OSSBOT_ARG_PLAN_FILE>:
#   {"commands": [{"name": "unit", "cmd": "go test ./pkg/...", "timeout_s": 1200}, ...]}
# Each command: bash -o pipefail -lc in /work/repo, offline env (Maven: online through the proxy, V-M6),
# its own timeout (<= 1200 s), output to
# /io/output/logs/<name>.log. Writes /io/output/validation.json and result.json.
set -euo pipefail

WORK="${OSSBOT_WORK:-/work}"
IO="${OSSBOT_IO:-/io}"
OUT="$IO/output"
PLAN_FILE="${OSSBOT_ARG_PLAN_FILE:-validate.json}"

fail() {
  echo "validate: $2" >&2
  printf '{"ok":false,"error_class":"%s"}\n' "$1" >"$OUT/result.json"
  exit 3
}

mkdir -p "$OUT/logs"
[[ "$PLAN_FILE" =~ ^[a-z0-9_-]{1,64}\.json$ ]] || fail bad_args "OSSBOT_ARG_PLAN_FILE"
PLAN="$IO/input/$PLAN_FILE"
[[ -f "$PLAN" && ! -L "$PLAN" ]] || fail bad_args "plan file missing"
COUNT="$(jq -e '.commands | length' "$PLAN")" || fail bad_plan "plan is not {commands: [...]}"
[[ "$COUNT" -le 50 ]] || fail bad_plan "more than 50 commands"

export HOME="$WORK/.home" LANG=C.UTF-8 GOFLAGS=-mod=mod GOPROXY=off npm_config_offline=true UV_OFFLINE=1 \
  PIP_NO_INDEX=1 GOMODCACHE="$WORK/cache/go/mod" GOCACHE="$WORK/cache/go/build" \
  npm_config_cache="$WORK/cache/npm" UV_CACHE_DIR="$WORK/cache/uv" GRADLE_USER_HOME="$WORK/cache/gradle" \
  MISE_DATA_DIR="$WORK/cache/mise" CHECKPOINT_DISABLE=1 VIRTUAL_ENV="$WORK/venv"
export PATH="$WORK/venv/bin:/opt/mise/shims:$PATH"
# ADR-0025: proxy-unaware tools reach the proxy via netshim; ssh GitHub URLs (submodules) use https
# shellcheck source=sandbox/entrypoints/lib/netshim.sh disable=SC1091
source "$(dirname -- "$0")/lib/netshim.sh"
ossbot_netshim_start
ossbot_gitconfig
# JVM tools (lib/jvm_env.sh): Gradle offline through its init script, Maven online through the proxy (its
# surefire providers resolve only when tests run, V-M6), the JDK homes; GRADLE_OPTS stays unset.
# shellcheck source=sandbox/entrypoints/lib/jvm_env.sh disable=SC1091
source "$(dirname -- "$0")/lib/jvm_env.sh"
ossbot_jvm_setup validate
cd "$WORK/repo"

RESULTS="$OUT/validation.ndjson"
: >"$RESULTS"
for ((i = 0; i < COUNT; i++)); do
  name="$(jq -r ".commands[$i].name" "$PLAN")"
  cmd="$(jq -r ".commands[$i].cmd" "$PLAN")"
  limit="$(jq -r ".commands[$i].timeout_s // 1200" "$PLAN")"
  [[ "$name" =~ ^[a-z0-9_.-]{1,64}$ ]] || fail bad_plan "command name"
  [[ "$limit" =~ ^[0-9]{1,4}$ && "$limit" -ge 1 && "$limit" -le 1200 ]] || fail bad_plan "timeout_s"
  [[ ${#cmd} -le 8192 && -n "$cmd" ]] || fail bad_plan "cmd"
  start=$SECONDS
  code=0
  timeout --kill-after=10 "$limit" bash -o pipefail -lc "$cmd" >"$OUT/logs/$name.log" 2>&1 </dev/null || code=$?
  jq -cn --arg name "$name" --argjson code "$code" --argjson secs "$((SECONDS - start))" \
    --arg tail "$(tail -c 4000 "$OUT/logs/$name.log")" \
    '{name: $name, exit_code: $code, duration_s: $secs, timed_out: ($code == 124), tail: $tail}' >>"$RESULTS"
done
jq -s '{commands: .}' "$RESULTS" >"$OUT/validation.json"
rm -f "$RESULTS"
printf '{"ok":true,"error_class":null}\n' >"$OUT/result.json"
