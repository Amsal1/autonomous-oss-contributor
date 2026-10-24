# shellcheck shell=bash
# Toolchain environment of the code-running entrypoints (build-env design §6.3, BE-4.3, BE-4.8). Sourced by
# fetch.sh, validate.sh and exec.sh (before lib/jvm_env.sh); not a job entrypoint.
#
# ossbot_tool_env <online|offline>   the shared exports: mise (task MISE_DATA_DIR, the image's tools as the shared
#                                    read-only fallback, never mise-versions.jdx.dev), Go (GOTOOLCHAIN=local: Go never
#                                    downloads a toolchain itself), Rust (RUSTUP_HOME/CARGO_HOME on the task volume),
#                                    corepack, uv's Python downloads; offline adds MISE_OFFLINE, RUSTUP_AUTO_INSTALL=0,
#                                    COREPACK_ENABLE_NETWORK=0 and UV_PYTHON_DOWNLOADS=never
# ossbot_offline_switches            GOPROXY=off npm_config_offline UV_OFFLINE PIP_NO_INDEX (validate's plan commands
#                                    outside OSSBOT_NET=web)
# ossbot_toolchains <install|select> <budget> [report]
#                                    run the resolver (sandbox_tools/toolchains.py) with the four offline switches
#                                    unset (C-N3: it fetches the ecosystem indexes, and its installers need the
#                                    registries); a failure is a stderr line, never a job failure
# ossbot_tc_read KEY                 the validated value of KEY in $HOME/.ossbot/toolchain.env (never sourced: each
#                                    line is matched against the §6.3 rules, anything else is ignored)
# ossbot_tool_paths <venv|novenv>    PATH = resolver prepend : $JAVA_HOME/bin [: $WORK/venv/bin] : mise shims : the
#                                    image's rustup proxies : the image PATH; RUSTUP_TOOLCHAIN from the env file

OSSBOT_BASE_PATH="${OSSBOT_BASE_PATH:-$PATH}"
TOOLCHAINS_BIN="${TOOLCHAINS_BIN:-/opt/ossbot/bin/ossbot-toolchains}"  # tests relocate it
PYTHON_MIRROR=https://github.com/astral-sh/python-build-standalone/releases/download
TC_VALUE_RE='^[A-Za-z0-9_./:@+,=-]{1,1024}$'
TC_JVM_RE='^/usr/lib/jvm/java-(8|11|17|21|25)-openjdk-amd64$'
TC_OPT_RE='^/opt/(mise|rust|maven|uv-python)(/[A-Za-z0-9._+-]{1,128}){0,6}$'
TC_NAME_RE='^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$'

ossbot_tool_env() {
  local mode="${1:-}"
  case "$mode" in
    online | offline) ;;
    *)
      echo "tool_env: unknown mode '${mode}'" >&2
      return 2
      ;;
  esac
  if [[ -z "${WORK:-}" || -z "${HOME:-}" ]]; then
    echo "tool_env: WORK and HOME must be set" >&2
    return 2
  fi
  export MISE_DATA_DIR="$WORK/cache/mise" MISE_SHARED_INSTALL_DIRS=/opt/mise/installs \
    MISE_SYSTEM_CONFIG_FILE=/opt/mise/config/config.toml MISE_GLOBAL_CONFIG_FILE="$HOME/.ossbot/mise.toml" \
    MISE_TRUSTED_CONFIG_PATHS="$WORK/repo" MISE_YES=1 MISE_USE_VERSIONS_HOST=false \
    MISE_USE_VERSIONS_HOST_TRACK=false MISE_RUBY_COMPILE=false GOTOOLCHAIN=local \
    RUSTUP_HOME="$WORK/cache/rustup" CARGO_HOME="$WORK/cache/cargo" COREPACK_HOME="$WORK/cache/corepack" \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 UV_PYTHON_INSTALL_DIR="$WORK/cache/uv-python" \
    UV_PYTHON_INSTALL_MIRROR="$PYTHON_MIRROR"
  if [[ "$mode" == online ]]; then
    export UV_PYTHON_DOWNLOADS=automatic
    unset MISE_OFFLINE RUSTUP_AUTO_INSTALL COREPACK_ENABLE_NETWORK
  else
    export UV_PYTHON_DOWNLOADS=never MISE_OFFLINE=1 RUSTUP_AUTO_INSTALL=0 COREPACK_ENABLE_NETWORK=0
  fi
  mkdir -p "$HOME/.ossbot"
  return 0
}

ossbot_offline_switches() {
  export GOPROXY=off npm_config_offline=true UV_OFFLINE=1 PIP_NO_INDEX=1
}

ossbot_toolchains() {
  local mode="${1:-}" budget="${2:-}" report="${3:-}" args=()
  [[ "$mode" =~ ^(install|select)$ && "$budget" =~ ^[0-9]{1,4}$ ]] || {
    echo "tool_env: ossbot_toolchains <install|select> <budget> [report]" >&2
    return 2
  }
  args=(--repo "$WORK/repo" --state "$HOME/.ossbot" --mode "$mode" --budget "$budget" --work "$WORK")
  [[ -n "$report" ]] && args+=(--report "$report")
  if ! env -u UV_OFFLINE -u PIP_NO_INDEX -u GOPROXY -u npm_config_offline \
    /usr/bin/python3 -I "$TOOLCHAINS_BIN" "${args[@]}"; then
    echo "tool_env: the toolchain resolver failed; the image defaults apply" >&2
  fi
  return 0
}

_ossbot_tc_path() { # a toolchain path of §6.3: an image JDK, under the task cache or the image's /opt toolchains
  local v=$1 cache_re
  [[ "/$v/" != */./* && "/$v/" != */../* ]] || return 1
  cache_re="^${WORK//./\\.}/cache/(mise|rustup|cargo|corepack|corepack-bin|uv-python)(/[A-Za-z0-9._+-]{1,128}){0,6}\$"
  [[ $v =~ $TC_JVM_RE || $v =~ $cache_re || $v =~ $TC_OPT_RE ]]
}

_ossbot_tc_valid() { # KEY VALUE: the same rule as toolchains.valid_env and execd
  local key=$1 value=$2 part
  [[ $value =~ $TC_VALUE_RE ]] || return 1
  case "$key" in
    RUSTUP_HOME) [[ $value == "$WORK/cache/rustup" ]] ;;
    CARGO_HOME) [[ $value == "$WORK/cache/cargo" ]] ;;
    COREPACK_HOME) [[ $value == "$WORK/cache/corepack" ]] ;;
    UV_PYTHON_INSTALL_DIR) [[ $value == "$WORK/cache/uv-python" ]] ;;
    UV_PYTHON_INSTALL_MIRROR) [[ $value == "$PYTHON_MIRROR" ]] ;;
    MISE_GLOBAL_CONFIG_FILE) [[ $value == "$WORK/.home/.ossbot/mise.toml" ]] ;;
    GOTOOLCHAIN) [[ $value == local ]] ;;
    RUSTUP_TOOLCHAIN) [[ $value =~ $TC_NAME_RE ]] ;;
    JAVA_HOME | OSSBOT_GRADLE_JAVA_HOME) _ossbot_tc_path "$value" ;;
    OSSBOT_JDKS | OSSBOT_PATH_PREPEND)
      local sep=, parts=()
      [[ $key == OSSBOT_PATH_PREPEND ]] && sep=:
      IFS="$sep" read -r -a parts <<<"$value"
      ((${#parts[@]} > 0)) || return 1
      [[ $value != *"$sep" ]] || return 1
      for part in "${parts[@]}"; do
        _ossbot_tc_path "$part" || return 1
      done
      ;;
    *) return 1 ;;
  esac
}

ossbot_tc_read() {
  local want=$1 file="$HOME/.ossbot/toolchain.env" line key value n=0
  [[ -f "$file" && ! -L "$file" ]] || return 0
  while IFS= read -r line || [[ -n $line ]]; do
    ((++n <= 64)) || break
    [[ $line =~ ^([A-Z_]{1,40})=(.*)$ ]] || continue
    key=${BASH_REMATCH[1]}
    value=${BASH_REMATCH[2]}
    [[ $key == "$want" ]] || continue
    if _ossbot_tc_valid "$key" "$value"; then
      printf '%s\n' "$value"
    fi
    return 0
  done <"$file"
  return 0
}

ossbot_tool_paths() {
  local prepend java venv="" toolchain
  prepend="$(ossbot_tc_read OSSBOT_PATH_PREPEND)"
  java="${JAVA_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}"
  case "${1:-}" in
    venv) venv="$WORK/venv/bin:" ;;
    novenv) ;;
    *)
      echo "tool_env: ossbot_tool_paths <venv|novenv>" >&2
      return 2
      ;;
  esac
  # the image's uv-managed Pythons (3.13) last, so uv and a requires-python can find them (python3 stays 3.12)
  local py extra=""
  for py in /opt/uv-python/cpython-3.*-linux-x86_64-gnu/bin; do
    [[ -d $py ]] && extra+=":$py"
  done
  export PATH="${prepend:+$prepend:}$java/bin:${venv}/opt/mise/shims:/opt/rust/cargo/bin:$OSSBOT_BASE_PATH$extra"
  toolchain="$(ossbot_tc_read RUSTUP_TOOLCHAIN)"
  if [[ -n "$toolchain" ]]; then
    export RUSTUP_TOOLCHAIN="$toolchain"
  else
    unset RUSTUP_TOOLCHAIN
  fi
  return 0
}
