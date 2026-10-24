#!/usr/bin/env bash
# agent-exec entrypoint (DESIGN §10.4; sandbox-full, no secrets, task volume rw). Its network is
# --network none, or (ADR-0024, implement_network) its own ossbot-t-<cid>-exec network in pool_fetch;
# sandboxd then sets OSSBOT_EXEC_NETWORK=web. Runs the deterministic executor until the pair is torn
# down. execd creates /run/exec/exec.sock (0600, uid 10001); sandboxd waits for State.Running, then
# starts agent-llm.
# The JVM files (lib/jvm_env.sh): offline = Gradle offline through its init script, Maven offline
# through settings.xml; online = both online through the proxy. execd's commands get the same
# JAVA_HOME/MAVEN_* (execd fixed_env, online or offline to match).
set -euo pipefail

NET=${OSSBOT_EXEC_NETWORK:-}
if [[ ! $NET =~ ^(web)?$ ]]; then
  echo "exec.sh: bad OSSBOT_EXEC_NETWORK" >&2
  exit 3
fi

export WORK=/work HOME=/work/.home GRADLE_USER_HOME=/work/cache/gradle
mkdir -p "$HOME" /work/cache
# ADR-0025: ssh GitHub URLs (submodules) use https; with web, proxy-unaware tools reach the proxy via netshim
# shellcheck source=sandbox/entrypoints/lib/netshim.sh disable=SC1091
source "$(dirname -- "$0")/lib/netshim.sh"
ossbot_gitconfig
# build-env design §6.3: the resolver installs what is missing with web, else only selects what fetch installed;
# execd reads the same toolchain.env for its commands (fixed_env).
# shellcheck source=sandbox/entrypoints/lib/tool_env.sh disable=SC1091
source "$(dirname -- "$0")/lib/tool_env.sh"
# shellcheck source=sandbox/entrypoints/lib/jvm_env.sh disable=SC1091
source "$(dirname -- "$0")/lib/jvm_env.sh"
if [[ $NET == web ]]; then
  ossbot_netshim_start
  ossbot_tool_env online
  ossbot_toolchains install 600 /io/output/toolchains.json
  ossbot_jvm_setup online
  NETWORK=web
else
  ossbot_tool_env offline
  ossbot_toolchains select 0 /io/output/toolchains.json
  ossbot_jvm_setup offline
  NETWORK=none
fi
exec /opt/ossbot/bin/ossbot-execd \
  --socket /run/exec/exec.sock --repo /work/repo --work /work --log /io/output/exec-log.jsonl \
  --network "$NETWORK"
