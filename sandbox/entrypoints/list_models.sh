#!/usr/bin/env bash
# Catalog entrypoint (DESIGN §10.3 M11; sandbox-llm, profile catalog, kiro_core egress only).
# No prompt, no agent, no inputs, no repo: `kiro-cli chat --list-models --format json` only.
# Output /io/output/models.json (schema: §23 item 9, parsed by the runner) and result.json.
set -euo pipefail

IO="${OSSBOT_IO:-/io}"
OUT="$IO/output"
export HOME=/home/agent
KIRO="${OSSBOT_KIRO_CLI:-/opt/kiro-cli/bin/kiro-cli}"

mkdir -p "$OUT" "$HOME/.kiro/settings" "$HOME/cwd"
cp /opt/ossbot/kiro/settings/cli.json "$HOME/.kiro/settings/cli.json"
cd "$HOME/cwd"
set +e
"$KIRO" chat --list-models --format json >"$OUT/models.json" 2>"$OUT/list_models.stderr" </dev/null
code=$?
set -e
if [[ $code -eq 0 ]]; then
  printf '{"ok":true,"error_class":null,"exit_code":0}\n' >"$OUT/result.json"
else
  printf '{"ok":false,"error_class":"list_models_exit_%s","exit_code":%s}\n' "$code" "$code" >"$OUT/result.json"
fi
exit "$code"
