#!/usr/bin/env bash
# agent-exec entrypoint (DESIGN §10.4; sandbox-full, --network none, no secrets, task volume rw).
# Runs the deterministic executor until the pair is torn down. execd creates
# /run/exec/exec.sock (0600, uid 10001); sandboxd waits for State.Running, then starts agent-llm.
set -euo pipefail

mkdir -p /work/.home /work/cache
exec /opt/ossbot/bin/ossbot-execd \
  --socket /run/exec/exec.sock --repo /work/repo --work /work --log /io/output/exec-log.jsonl
