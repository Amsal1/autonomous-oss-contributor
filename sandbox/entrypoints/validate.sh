#!/usr/bin/env bash
# Validate job (DESIGN §9.9; sandbox-full, profile validate, registries via the proxy, no secrets,
# task volume rw). Runs the runner-composed plan /io/input/<OSSBOT_ARG_PLAN_FILE>:
#   {"commands": [{"name": "unit", "cmd": "go test ./pkg/...", "timeout_s": 1200}, ...]}
# Each command: bash -o pipefail -lc in /work/repo, offline env (Maven: online through the proxy, V-M6; with
# OSSBOT_NET=web everything online, build-env design §8.2), the repo's toolchains (lib/tool_env.sh),
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

export HOME="$WORK/.home" LANG=C.UTF-8 GOFLAGS=-mod=mod GOMODCACHE="$WORK/cache/go/mod" \
  GOCACHE="$WORK/cache/go/build" npm_config_cache="$WORK/cache/npm" UV_CACHE_DIR="$WORK/cache/uv" \
  GRADLE_USER_HOME="$WORK/cache/gradle" CHECKPOINT_DISABLE=1 VIRTUAL_ENV="$WORK/venv"
# build-env design §6.3, §8.2: the shared toolchain exports (validate always has a network: base or web). The
# plan's commands keep the offline switches unless OSSBOT_NET=web (sandboxd's admission: the public web).
# shellcheck source=sandbox/entrypoints/lib/tool_env.sh disable=SC1091
source "$(dirname -- "$0")/lib/tool_env.sh"
ossbot_tool_env online
[[ "${OSSBOT_NET:-base}" == web ]] || ossbot_offline_switches
# ADR-0025: proxy-unaware tools reach the proxy via netshim; ssh GitHub URLs (submodules) use https
# shellcheck source=sandbox/entrypoints/lib/netshim.sh disable=SC1091
source "$(dirname -- "$0")/lib/netshim.sh"
ossbot_netshim_start
ossbot_gitconfig
# The resolver again (install, 600 s): fetch installed the declared toolchains already; this picks them up and
# installs only what a later change needs. C-N3: ossbot_toolchains runs it with the four offline switches unset
# in every mode (env -u UV_OFFLINE -u PIP_NO_INDEX -u GOPROXY -u npm_config_offline); the plan's commands below
# still see them outside OSSBOT_NET=web.
ossbot_toolchains install 600 "$OUT/toolchains.json"
# JVM tools (lib/jvm_env.sh): the resolver's JDKs; Gradle offline through its init script (online with web),
# Maven online through the proxy (its surefire providers resolve only when tests run, V-M6); GRADLE_OPTS unset.
# shellcheck source=sandbox/entrypoints/lib/jvm_env.sh disable=SC1091
source "$(dirname -- "$0")/lib/jvm_env.sh"
ossbot_jvm_setup validate
ossbot_tool_paths venv
cd "$WORK/repo"
# Go workspace mode (go.work) refuses GOFLAGS=-mod=mod ("set GOWORK=off to disable workspace mode")
if [[ -f go.work ]]; then unset GOFLAGS; fi

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
# toolchain_gaps (§8.2): what the resolver could not install; the runner reports them as "Sandbox gap: …"
GAPS='[]'
if [[ -f "$OUT/toolchains.json" && ! -L "$OUT/toolchains.json" ]]; then
  GAPS="$(jq -c '[.gaps[]? | strings | .[0:200]][:10]' "$OUT/toolchains.json" 2>/dev/null)" || GAPS='[]'
  [[ -n "$GAPS" ]] || GAPS='[]'
fi
jq -s --argjson gaps "$GAPS" '{commands: ., toolchain_gaps: $gaps}' "$RESULTS" >"$OUT/validation.json"
rm -f "$RESULTS"
printf '{"ok":true,"error_class":null}\n' >"$OUT/result.json"
