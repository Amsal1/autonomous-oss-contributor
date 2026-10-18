#!/usr/bin/env bash
# Fetch job (DESIGN §9.6; sandbox-full, profile fetch: github_read + package registries via the
# proxy, no secrets, fresh task volume at /work). Parameters from sandboxd's fixed env:
# OSSBOT_ARG_REPO (owner/name), OSSBOT_ARG_BASE_REF (optional; default branch otherwise),
# OSSBOT_ARG_BASE_SHA (optional, import M3: requires BASE_REF), OSSBOT_ARG_CLONE_DEPTH.
# Writes /io/output/fetch.json and result.json {"ok", "error_class", "head_sha"}.
set -euo pipefail

WORK="${OSSBOT_WORK:-/work}"
IO="${OSSBOT_IO:-/io}"
OUT="$IO/output"
REPO="$WORK/repo"
SLUG="${OSSBOT_ARG_REPO:-}"
BASE_REF="${OSSBOT_ARG_BASE_REF:-}"
BASE_SHA="${OSSBOT_ARG_BASE_SHA:-}"
DEPTH="${OSSBOT_ARG_CLONE_DEPTH:-200}"
# OSSBOT_ARG_PREFETCH=0: the CR-004 scout clone (shallow, no dependency prefetch, so no repo code
# runs). It is marked, and the real fetch after Start work replaces it with a fresh clone.
PREFETCH_ON="${OSSBOT_ARG_PREFETCH:-1}"
SCOUT_MARK="$WORK/.scout-clone"

result() { # result <ok> <error_class|null> [head]
  local err_json="null" head_json="null"
  [[ "$2" != "null" ]] && err_json="\"$2\""
  [[ -n "${3:-}" ]] && head_json="\"$3\""
  printf '{"ok":%s,"error_class":%s,"head_sha":%s}\n' "$1" "$err_json" "$head_json" >"$OUT/result.json"
}
fail() { echo "fetch: $2" >&2; result false "$1"; exit 3; }

mkdir -p "$OUT"
[[ "$SLUG" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$ ]] || fail bad_args "OSSBOT_ARG_REPO"
[[ -z "$BASE_REF" || "$BASE_REF" =~ ^[A-Za-z0-9._/-]{1,100}$ ]] || fail bad_args "OSSBOT_ARG_BASE_REF"
[[ -z "$BASE_SHA" || "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail bad_args "OSSBOT_ARG_BASE_SHA"
[[ -z "$BASE_SHA" || -n "$BASE_REF" ]] || fail bad_args "BASE_SHA requires BASE_REF"
[[ "$DEPTH" =~ ^[1-9][0-9]{0,4}$ ]] || fail bad_args "OSSBOT_ARG_CLONE_DEPTH"
[[ "$PREFETCH_ON" =~ ^[01]$ ]] || fail bad_args "OSSBOT_ARG_PREFETCH"
if [[ -f "$SCOUT_MARK" ]]; then rm -rf "$REPO" "$SCOUT_MARK"; fi  # the scout clone is not the build clone

mkdir -p "$REPO" "$WORK/cache" "$WORK/venv" "$WORK/.home"
export HOME="$WORK/.home" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0
G=(git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.file.allow=never
   -c protocol.ext.allow=never -c submodule.recurse=false)

BRANCH=()
[[ -n "$BASE_REF" ]] && BRANCH=(--branch "$BASE_REF")
"${G[@]}" clone -q --single-branch "${BRANCH[@]}" --depth "$DEPTH" --no-tags --no-recurse-submodules \
  "https://github.com/$SLUG.git" "$REPO" || fail clone_failed "git clone failed"
cd "$REPO"
"${G[@]}" config core.hooksPath /dev/null  # defence in depth; package and gatekeeper never trust it
[[ -n "$BASE_REF" ]] || BASE_REF="$("${G[@]}" symbolic-ref --short HEAD)"
if [[ -n "$BASE_SHA" ]]; then
  if ! "${G[@]}" cat-file -e "$BASE_SHA^{commit}" 2>/dev/null; then
    "${G[@]}" fetch -q --depth 1 --no-tags origin "$BASE_SHA" || fail base_missing "base_sha not fetchable"
  fi
  "${G[@]}" checkout -q --detach "$BASE_SHA"
fi
HEAD_SHA="$("${G[@]}" rev-parse HEAD)"
[[ -z "$BASE_SHA" || "$HEAD_SHA" == "$BASE_SHA" ]] || fail base_mismatch "checked out $HEAD_SHA"

# Ecosystem detection and best-effort dependency prefetch (failures are recorded, not fatal).
export GOMODCACHE="$WORK/cache/go/mod" GOCACHE="$WORK/cache/go/build" GOTOOLCHAIN=auto GOFLAGS=-mod=mod \
  npm_config_cache="$WORK/cache/npm" UV_CACHE_DIR="$WORK/cache/uv" PIP_CACHE_DIR="$WORK/cache/pip" \
  GRADLE_USER_HOME="$WORK/cache/gradle" MISE_DATA_DIR="$WORK/cache/mise" CHECKPOINT_DISABLE=1 \
  UV_PROJECT_ENVIRONMENT="$WORK/venv"
ECOS=()
PREFETCH=()
prefetch() { # prefetch <name> <cmd...>
  local name=$1 code=0
  [[ "$PREFETCH_ON" == 1 ]] || return 0
  shift
  timeout 900 "$@" >"$OUT/prefetch-$name.log" 2>&1 || code=$?
  PREFETCH+=("$name=$code")
}
has() { [[ -n "$(find . -maxdepth 3 -name "$1" -not -path './.git/*' -print -quit)" ]]; }

if [[ -f .tool-versions || -f mise.toml || -f .mise.toml ]]; then ECOS+=(mise); prefetch mise mise install; fi
if [[ -f go.mod ]]; then ECOS+=(go); prefetch go go mod download; fi
if [[ -f package.json ]]; then
  ECOS+=(node)
  if [[ -f pnpm-lock.yaml ]]; then prefetch pnpm corepack pnpm install --frozen-lockfile
  elif [[ -f yarn.lock ]]; then prefetch yarn corepack yarn install --immutable
  elif [[ -f package-lock.json ]]; then prefetch npm npm ci
  fi
fi
if [[ -f pyproject.toml || -n "$(find . -maxdepth 1 -name 'requirements*.txt' -print -quit)" ]]; then
  ECOS+=(python)
  if [[ -f uv.lock ]]; then prefetch uv uv sync --frozen
  elif [[ -f pyproject.toml ]]; then
    prefetch venv uv venv "$WORK/venv"
    prefetch pip uv pip install --python "$WORK/venv/bin/python" -e ".[test,dev]"
  fi
fi
if [[ -f build.gradle || -f build.gradle.kts ]]; then
  ECOS+=(gradle)
  [[ -x ./gradlew ]] && prefetch gradle ./gradlew --no-daemon dependencies
fi
if has '*.tf'; then ECOS+=(terraform); prefetch tofu tofu init -backend=false -input=false; fi
if has Chart.yaml; then
  ECOS+=(helm)
  chart_dir="$(dirname "$(find . -maxdepth 3 -name Chart.yaml -not -path './.git/*' -print -quit)")"
  prefetch helm helm dependency build "$chart_dir"
fi
if [[ -f Cargo.toml ]]; then ECOS+=(rust); prefetch cargo cargo fetch; fi

AGENT_CONFIG=()
for f in .kiro .amazonq AGENTS.md .cursorrules CLAUDE.md; do [[ -e "$f" ]] && AGENT_CONFIG+=("$f"); done

# Test-suite detection for the tests-required rule (§9.14, AC 41): tracked paths that look like
# tests or test-runner config. Deterministic, so the review backstop never trusts model output
# for "this repo has no suite". Same pattern as ossbot.runner.stages.common.TEST_PATH_RE.
TEST_RE='(^|/)(tests?|specs?|__tests__|testing)/|(^|/)test_[^/]*\.py$|_test\.[A-Za-z0-9]+$|\.(test|spec)\.[A-Za-z0-9]+$|(^|/)(pytest\.ini|conftest\.py|jest\.config\.[A-Za-z]+|vitest\.config\.[A-Za-z]+|karma\.conf\.js|phpunit\.xml(\.dist)?)$'
TEST_FILES="$("${G[@]}" ls-files -z | tr '\0' '\n' | grep -Ec "$TEST_RE" || true)"
[[ "$TEST_FILES" =~ ^[0-9]+$ ]] || TEST_FILES=0

jq -n --arg base_sha "$HEAD_SHA" --arg base_ref "$BASE_REF" --argjson test_files "$TEST_FILES" \
  --arg ecos "${ECOS[*]:-}" --arg prefetch "${PREFETCH[*]:-}" --arg agent "${AGENT_CONFIG[*]:-}" \
  '{base_sha: $base_sha, base_ref: $base_ref, test_files: $test_files, has_test_suite: ($test_files > 0),
    ecosystems: ($ecos | split(" ") | map(select(. != ""))),
    prefetch: ($prefetch | split(" ") | map(select(. != "") | split("=") | {(.[0]): (.[1] | tonumber)}) | add // {}),
    agent_config: ($agent | split(" ") | map(select(. != "")))}' >"$OUT/fetch.json"
[[ "$PREFETCH_ON" == 1 ]] || : >"$SCOUT_MARK"
result true null "$HEAD_SHA"
