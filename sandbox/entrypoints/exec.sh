#!/usr/bin/env bash
# agent-exec entrypoint (DESIGN §10.4; sandbox-full, --network none, no secrets, task volume rw).
# Runs the deterministic executor until the pair is torn down. execd creates
# /run/exec/exec.sock (0600, uid 10001); sandboxd waits for State.Running, then starts agent-llm.
# The JVM files (lib/jvm_env.sh, mode offline): Gradle offline through its init script, Maven offline
# through settings.xml, the JDK homes. execd's commands get the same JAVA_HOME/MAVEN_* (execd fixed_env).
set -euo pipefail

export WORK=/work HOME=/work/.home GRADLE_USER_HOME=/work/cache/gradle
mkdir -p "$HOME" /work/cache
# shellcheck source=sandbox/entrypoints/lib/jvm_env.sh disable=SC1091
source "$(dirname -- "$0")/lib/jvm_env.sh"
ossbot_jvm_setup offline
exec /opt/ossbot/bin/ossbot-execd \
  --socket /run/exec/exec.sock --repo /work/repo --work /work --log /io/output/exec-log.jsonl
