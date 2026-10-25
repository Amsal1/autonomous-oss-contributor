#!/usr/bin/env bash
# Fetch job (DESIGN §9.6; sandbox-full, profile fetch: github_read + package registries via the
# proxy, no secrets, fresh task volume at /work). Parameters from sandboxd's fixed env:
# OSSBOT_ARG_REPO (owner/name), OSSBOT_ARG_BASE_REF (optional; default branch otherwise),
# OSSBOT_ARG_BASE_SHA (optional, import M3: requires BASE_REF), OSSBOT_ARG_CLONE_DEPTH,
# OSSBOT_ARG_REFETCH (optional, 1 = the implement stage's one re-fetch, DESIGN §9.8).
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
# OSSBOT_ARG_PREFETCH=0: the CR-004 scout clone before gate 1 (DESIGN §9.5a, ADR-0005 addendum):
# depth 1, single branch, no tags, no submodules, no LFS smudge, https only, no hooks, no
# credentials, nothing from the repo runs (no dependency fetch or build). It is size-capped
# (SCOUT_MAX_MB, error_class snapshot_too_large) and marked; the runner destroys the task volume
# when the scout stage ends, and the real fetch after Start work starts from a fresh clone.
PREFETCH_ON="${OSSBOT_ARG_PREFETCH:-1}"
# OSSBOT_ARG_REFETCH=1: the implement stage's one re-fetch after the coder asked for a new dependency
# (DESIGN §9.8). It keeps the working tree (no rm, no clone; any checkout, also the detached HEAD of fix
# rounds and imports), runs the same detection and prefetch, and lists the dependency files the coder
# changed in fetch.json deps_changed. It runs LLM-edited repo code in the fetch profile, as validate does.
REFETCH="${OSSBOT_ARG_REFETCH:-0}"
SCOUT_MARK="$WORK/.scout-clone"
SCOUT_MAX_MB=2048  # same cap as the research snapshot (snapshot.sh); ossbot.runner.stages.scout.SCOUT_MAX_MB

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
[[ "$REFETCH" =~ ^[01]$ ]] || fail bad_args "OSSBOT_ARG_REFETCH"
[[ "$REFETCH" == 0 || ("$PREFETCH_ON" == 1 && -z "$BASE_REF" && -z "$BASE_SHA") ]] ||
  fail bad_args "OSSBOT_ARG_REFETCH takes no base and needs the prefetch"
# ADR-0025: proxy-unaware tools (a JVM, curl in a build script, installers) reach the proxy via netshim
# shellcheck source=sandbox/entrypoints/lib/netshim.sh disable=SC1091
source "$(dirname -- "$0")/lib/netshim.sh"
ossbot_netshim_start
# build-env design §6: the toolchain environment and the per-repo resolver (lib/tool_env.sh)
# shellcheck source=sandbox/entrypoints/lib/tool_env.sh disable=SC1091
source "$(dirname -- "$0")/lib/tool_env.sh"
# shellcheck source=sandbox/entrypoints/lib/jvm_env.sh disable=SC1091
source "$(dirname -- "$0")/lib/jvm_env.sh"
cd "$WORK"  # the container may start inside /work/repo: never delete the working directory
if [[ "$REFETCH" == 1 ]]; then
  [[ -d "$REPO/.git" && ! -L "$REPO/.git" ]] || fail refetch_no_repo "no working tree to re-fetch for"
else
  # Every fetch starts from a fresh clone: a scout clone is not the build clone, a re-queued
  # fetch finds a partial clone, and a new version after a rescope finds the old build tree.
  rm -rf "$REPO" "$SCOUT_MARK"
fi

mkdir -p "$REPO" "$WORK/cache" "$WORK/venv" "$WORK/.home"
export HOME="$WORK/.home" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0
unset GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS GIT_ASKPASS SSH_ASKPASS
[[ "$PREFETCH_ON" == 1 ]] || export GIT_LFS_SKIP_SMUDGE=1
G=(git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.allow=never -c protocol.https.allow=always
   -c submodule.recurse=false -c credential.helper=)  # no system/global config: no LFS filter is defined

DEPS_CHANGED=()
if [[ "$REFETCH" == 0 ]]; then
  BRANCH=()
  [[ -n "$BASE_REF" ]] && BRANCH=(--branch "$BASE_REF")
  "${G[@]}" clone -q --single-branch "${BRANCH[@]}" --depth "$DEPTH" --no-tags --no-recurse-submodules \
    "https://github.com/$SLUG.git" "$REPO" || fail clone_failed "git clone failed"
  cd "$REPO"
  "${G[@]}" config core.hooksPath /dev/null  # defence in depth; package and gatekeeper never trust it
  if [[ "$PREFETCH_ON" == 0 ]]; then
    SIZE_MB="$(du -s --block-size=1M "$REPO" | cut -f1)"
    if (( SIZE_MB > SCOUT_MAX_MB )); then
      cd /
      rm -rf "$REPO"
      fail snapshot_too_large "scout clone is ${SIZE_MB} MB > ${SCOUT_MAX_MB} MB"
    fi
  fi
  [[ -n "$BASE_REF" ]] || BASE_REF="$("${G[@]}" symbolic-ref --short HEAD)"
  if [[ -n "$BASE_SHA" ]]; then
    if ! "${G[@]}" cat-file -e "$BASE_SHA^{commit}" 2>/dev/null; then
      "${G[@]}" fetch -q --depth 1 --no-tags origin "$BASE_SHA" || fail base_missing "base_sha not fetchable"
    fi
    "${G[@]}" checkout -q --detach "$BASE_SHA"
  fi
  HEAD_SHA="$("${G[@]}" rev-parse HEAD)"
  [[ -z "$BASE_SHA" || "$HEAD_SHA" == "$BASE_SHA" ]] || fail base_mismatch "checked out $HEAD_SHA"
  # The scout's repository tree (git ls-files only, nothing from the repo runs; §9.5a rule 6). Non-fatal.
  bash "$(dirname -- "$0")/lib/repo_tree.sh" "$REPO" "$OUT/repo-tree.txt" || rm -f -- "$OUT/repo-tree.txt"
else
  cd "$REPO"
  HEAD_SHA="$("${G[@]}" rev-parse HEAD)" || fail refetch_no_repo "no HEAD to re-fetch for"
  # A detached HEAD (fix rounds, imports) has no branch: base_ref is null, never a set -e exit.
  BASE_REF="$("${G[@]}" symbolic-ref -q --short HEAD || true)"
  # The dependency files the coder changed (fixed basenames; untracked ones too). Untrusted for the runner.
  while IFS= read -r -d '' entry; do
    xy="${entry:0:2}"
    path="${entry:3}"
    if [[ "$xy" == *[RC]* ]]; then IFS= read -r -d '' _ || true; fi  # -z: a rename's source path follows
    # build-env design §8.2: the toolchain declarations too, so a changed one gets its toolchain installed
    case "${path##*/}" in
      package.json | package-lock.json | pnpm-lock.yaml | yarn.lock | go.mod | go.sum | build.gradle | \
        build.gradle.kts | settings.gradle | settings.gradle.kts | gradle.properties | libs.versions.toml | \
        gradle.lockfile | pom.xml | pyproject.toml | uv.lock | poetry.lock | requirements*.txt | Cargo.toml | \
        Cargo.lock | Gemfile | Gemfile.lock | Chart.yaml | Chart.lock | .tool-versions | mise.toml | .mise.toml | \
        .java-version | .sdkmanrc | .nvmrc | .node-version | .python-version | rust-toolchain | \
        rust-toolchain.toml | .terraform-version | .ruby-version | gradle-wrapper.properties | \
        maven-wrapper.properties | gradle-daemon-jvm.properties)
        DEPS_CHANGED+=("$path")
        ;;
    esac
  done < <("${G[@]}" status --porcelain=v1 -z --untracked-files=all)
fi

# Ecosystem detection and best-effort dependency prefetch (failures are recorded, not fatal).
export GOMODCACHE="$WORK/cache/go/mod" GOCACHE="$WORK/cache/go/build" GOFLAGS=-mod=mod \
  npm_config_cache="$WORK/cache/npm" UV_CACHE_DIR="$WORK/cache/uv" PIP_CACHE_DIR="$WORK/cache/pip" \
  GRADLE_USER_HOME="$WORK/cache/gradle" CHECKPOINT_DISABLE=1 UV_PROJECT_ENVIRONMENT="$WORK/venv"
# Go workspace mode (go.work) refuses GOFLAGS=-mod=mod ("set GOWORK=off to disable workspace mode")
if [[ -f "$WORK/repo/go.work" ]]; then unset GOFLAGS; fi
ossbot_tool_env online  # MISE_DATA_DIR, GOTOOLCHAIN=local, RUSTUP_HOME, CARGO_HOME, COREPACK_HOME, uv Python
# The repo's declared toolchains (JDK, Node + its package manager, Python, Go, Rust, Terraform, Ruby, the repo's
# own mise config) into the task cache, within 1800 s (§6.1); then the JVM tools, which ignore HTTPS_PROXY:
# lib/jvm_env.sh writes it into gradle.properties and settings.xml (online mode: no offline init script).
# Never in scout mode, which runs nothing from the repo.
if [[ "$PREFETCH_ON" == 1 ]]; then
  ossbot_toolchains install 1800 "$OUT/toolchains.json"
  ossbot_jvm_setup online
  ossbot_tool_paths novenv
fi
ECOS=()
PREFETCH=()
prefetch() { # prefetch <name> <cmd...> (Gradle and Maven warm-ups get 1800 s, BE-5.5)
  local name=$1 code=0 limit=900
  [[ "$PREFETCH_ON" == 1 ]] || return 0
  shift
  [[ "$name" == gradle || "$name" == maven* ]] && limit=1800
  timeout "$limit" "$@" >"$OUT/prefetch-$name.log" 2>&1 || code=$?
  PREFETCH+=("$name=$code")
}
has() { [[ -n "$(find . -maxdepth 3 -name "$1" -not -path './.git/*' -print -quit)" ]]; }

# mise: the resolver ran the repo's own `mise install` (its report entry "mise" has the exit code)
if [[ -f .tool-versions || -f mise.toml || -f .mise.toml ]]; then ECOS+=(mise); fi
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
# Gradle: the whole build once (every resolvable configuration of every project, the plugins and the
# wrapper distribution, sandbox/gradle/ossbot-prefetch.init.gradle). `bash` survives a lost exec bit.
if [[ -f build.gradle || -f build.gradle.kts || -f settings.gradle || -f settings.gradle.kts ]]; then
  ECOS+=(gradle)
  [[ -f ./gradlew ]] && prefetch gradle bash ./gradlew --no-daemon --no-configuration-cache -I /opt/ossbot/gradle/ossbot-prefetch.init.gradle ossbotPrefetch
fi
# Maven: the repo's wrapper, else the image's mvn; go-offline (plugins and dependencies) and test-compile
# (the compiler plugin, annotation processors, the reactor). Surefire's providers resolve only when tests
# run (V-M6): validate runs Maven online through the proxy and fills /work/cache/m2.
if [[ -f pom.xml ]]; then
  ECOS+=(maven)
  MVN=(mvn)
  [[ -f ./mvnw ]] && MVN=(bash ./mvnw)
  prefetch maven "${MVN[@]}" -B -ntp org.apache.maven.plugins:maven-dependency-plugin:3.11.0:go-offline
  prefetch maven-compile "${MVN[@]}" -B -ntp test-compile
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

# prefetch_failed: the dependency prefetches that exited non-zero (helm, tofu, venv: informational).
DEPS_JSON="$(jq -cn '$ARGS.positional' --args "${DEPS_CHANGED[@]}")"
# The resolver's report, compact (the runner shows it to the coder as untrusted facts, capped, §6.1).
TOOLCHAINS_JSON='[]'
if [[ -f "$OUT/toolchains.json" && ! -L "$OUT/toolchains.json" ]]; then
  TOOLCHAINS_JSON="$(jq -c '[.tools[]? | {tool, declared, source, chosen, action, gap}
    | with_entries(select(.value != null))][:40]' "$OUT/toolchains.json" 2>/dev/null)" || TOOLCHAINS_JSON='[]'
  [[ -n "$TOOLCHAINS_JSON" ]] || TOOLCHAINS_JSON='[]'
fi
jq -n --arg base_sha "$HEAD_SHA" --arg base_ref "$BASE_REF" --argjson test_files "$TEST_FILES" \
  --arg ecos "${ECOS[*]:-}" --arg prefetch "${PREFETCH[*]:-}" --arg agent "${AGENT_CONFIG[*]:-}" \
  --arg refetch "$REFETCH" --argjson deps "$DEPS_JSON" --argjson toolchains "$TOOLCHAINS_JSON" \
  '($prefetch | split(" ") | map(select(. != "") | split("=") | {(.[0]): (.[1] | tonumber)}) | add // {}) as $pf
   | {base_sha: $base_sha, base_ref: $base_ref, test_files: $test_files, has_test_suite: ($test_files > 0),
      ecosystems: ($ecos | split(" ") | map(select(. != ""))),
      prefetch: $pf,
      prefetch_failed: ($pf | to_entries | map(select(.value != 0 and (.key | IN("gradle", "maven",
        "maven-compile", "go", "npm", "pnpm", "yarn", "uv", "pip", "cargo"))) | .key)),
      agent_config: ($agent | split(" ") | map(select(. != ""))), toolchains: $toolchains}
   + (if $refetch == "1" then {refetch: true, deps_changed: $deps,
        base_ref: (if $base_ref == "" then null else $base_ref end)} else {} end)' >"$OUT/fetch.json"
[[ "$PREFETCH_ON" == 1 ]] || : >"$SCOUT_MARK"
result true null "$HEAD_SHA"
