#!/usr/bin/env bash
# Kiro headless entrypoint (DESIGN §15.3; sandbox-llm, profiles research and agent-llm).
# Every parameter comes from sandboxd's fixed env (never caller-chosen flags): ROLE, PROMPT_FILE,
# MODEL, EFFORT, TRUST_TOOLS (fixed per role), REQUIRE_MCP, OSSBOT_TRUST_MODE (ids|all, from the
# policy snapshot), OSSBOT_PROMPT_DIR (/io/input, or /opt/ossbot/probes for internal probe jobs),
# OSSBOT_LIST_REPO=1 (symlink probe: list the snapshot first). kiro-cli runs with the tmpfs HOME
# and the empty tmpfs cwd /home/agent/cwd, never the repo, so repo-local .kiro/ is not loaded.
set -euo pipefail

IO="${OSSBOT_IO:-/io}"
OUT="$IO/output"
export HOME=/home/agent
KIRO="${OSSBOT_KIRO_CLI:-/opt/kiro-cli/bin/kiro-cli}"

result() { # result <ok> <error_class|null> <exit>
  local err_json="null"
  [[ "$2" != "null" ]] && err_json="\"$2\""
  printf '{"ok":%s,"error_class":%s,"exit_code":%s}\n' "$1" "$err_json" "$3" >"$OUT/result.json"
}
fail() { echo "kiro: $2" >&2; result false "$1" 2; exit 2; }

mkdir -p "$OUT"
[[ "${ROLE:-}" =~ ^(vetter|planner|reviewer|summarizer|coder)$ ]] || fail bad_args "ROLE"
[[ "${PROMPT_FILE:-}" =~ ^[a-z0-9_-]{1,64}\.md$ ]] || fail bad_args "PROMPT_FILE"
[[ "${MODEL:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$ ]] || fail bad_args "MODEL"
[[ "${EFFORT:-}" =~ ^(low|medium|high|xhigh|max)$ ]] || fail bad_args "EFFORT"
[[ "${TRUST_TOOLS:-}" =~ ^[A-Za-z0-9@/_.,-]{1,1000}$ ]] || fail bad_args "TRUST_TOOLS"
PROMPT_DIR="${OSSBOT_PROMPT_DIR:-$IO/input}"
[[ "$PROMPT_DIR" == "$IO/input" || "$PROMPT_DIR" == /opt/ossbot/probes ]] || fail bad_args "OSSBOT_PROMPT_DIR"
PROMPT="$PROMPT_DIR/$PROMPT_FILE"
[[ -f "$PROMPT" && ! -L "$PROMPT" ]] || fail bad_args "prompt file missing"

# Baked settings, permissions and agents onto the tmpfs HOME (M1); prompts referenced by the agents.
mkdir -p "$HOME/.kiro/settings" "$HOME/.kiro/agents" "$HOME/cwd"
cp /opt/ossbot/kiro/settings/cli.json /opt/ossbot/kiro/settings/permissions.yaml "$HOME/.kiro/settings/"
find /opt/ossbot/kiro/agents -maxdepth 1 -name 'ossbot-*.json' -type f -exec cp {} "$HOME/.kiro/agents/" \;

if [[ "${OSSBOT_LIST_REPO:-}" == 1 ]]; then
  find /work/repo -mindepth 1 -printf '%y %P\n' | LC_ALL=C sort >"$OUT/repo-listing.txt"
fi

TRUST=("--trust-tools=$TRUST_TOOLS")
[[ "${OSSBOT_TRUST_MODE:-ids}" == all ]] && TRUST=(--trust-all-tools)
MCP=()
[[ "${REQUIRE_MCP:-}" == 1 ]] && MCP=(--require-mcp-startup)

cd "$HOME/cwd"
set +e
"$KIRO" chat --v3 --no-interactive \
  --agent "ossbot-$ROLE" --model "$MODEL" --effort "$EFFORT" \
  "${TRUST[@]}" "${MCP[@]}" \
  --output-format stream-json \
  <"$PROMPT" >"$OUT/$ROLE.events.jsonl" 2>"$OUT/$ROLE.stderr"
code=$?
set -e
if [[ $code -eq 0 ]]; then
  result true null 0
else
  result false "kiro_exit_$code" "$code"
fi
exit "$code"
