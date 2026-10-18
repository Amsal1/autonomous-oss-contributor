# ADR-0011: Kiro CLI version pinning and explicit V3 engine for headless and ACP
Status: Accepted (revised in design rev 1) · Date: 2026-10-17 · Design: DESIGN.md §15.2, §15.3

## Context
Kiro CLI 2.x ships two harnesses (V2 classic, V3). The release manifest listed 2.32.0 on 2026-10-17 (re-checked) with per-package sha256. CLI 3.0 is expected to make V3 default at the end of October 2026. V3 headless adds permissions.yaml, stream-json, `--sandbox`, `--require-mcp-startup`, exit code 4 for missing agents, and recommends `--trust-tools` with exact ids over `--trust-all-tools`. V3 ACP rejects v2 launch flags.

## Decision
- Install the headless tarball for a pinned version, verify sha256 from the manifest, record in `config/versions.lock.yaml`. kiro-cli is installed only in the `sandbox-llm` image and for user `hermes`, never in code-running images. Upgrade only via `ossctl kiro upgrade` (new image tag + doctor smoke + Hermes ACP round-trip).
- Pipeline: `kiro-cli chat --v3 --no-interactive --agent ossbot-<role> --model --effort --trust-tools=<fixed per-role ids> --output-format stream-json`, `--require-mcp-startup` for the coder; `--trust-all-tools` only as fallback if exact ids cannot be established (tool limits still come from agent definitions and deny rules).
- Hermes: `kiro-cli acp --agent-engine=v3` primary (ADR-0002); V2 ACP only as fallback.

## Consequences
Re-verify flags, tool ids, and engine behaviour when CLI 3.0 ships.

## Implementation addendum (FEAT-001, 2026-10-17)
- Re-fetched `desktop-release.q.us-east-1.amazonaws.com/latest/manifest.json`: latest is still **2.32.0**. Headless `2.32.0/kirocli-x86_64-linux.tar.gz` sha256 `05989f9bfe465b304e8d1b2dba50aec2b84fcc1deaa4c0773af3f539d6e0d312` (unchanged); also listed: `.tar.xz` `9d1527e4…08f3`, `.tar.zst` `81f95ec6…60ee`, musl variants. CLI 3.0 is not released yet; §23 item 12 stays open.

## Implementation addendum (FEAT-006 item 32, 2026-10-18): kiro-cli 2.32.0 V3 headless, verified live with the sandbox key

Found by the first live safety-probe run (the probes did their job: every failure below was fail-closed) and fixed before any pipeline Kiro job ran:

- **Engine extraction.** `chat --v3` extracts a node binary and the KAS bundle (KAS 0.66.43, ~270 MB) into `$HOME/.local/share/kiro-cli` and executes node from there. Job HOMEs are `noexec` tmpfs, so the first run failed with `failed to spawn the v3 engine`. The `sandbox-llm` build now extracts the engine once, offline (`RUN --network=none`, a placeholder key that never leaves the build), into root-owned read-only `/opt/kiro-cli/engine`; `kiro.sh` links it into each job's HOME. HOME stays `noexec`.
- **Agent format.** The V3 engine rejects V2-style agents ("needs upgrading for this agent engine, using default"). `.kiro/agents/ossbot-*.json` were converted with the pinned CLI's own `kiro-cli agent upgrade` (universal format: the V2 fields plus `permissions.rules`), then the converter's catch-all `fs_read allow` rule was removed so reads stay limited to the role's paths. Absolute `file://` prompts are refused by V3 (`PromptFileError`), so the agent `prompt` is a one-line pointer and the runner inlines `prompts/<role>.md` into every task (`runner/prompts.py task()`).
- **Fallback is fatal.** When the engine replaces a role agent by `default` (a `runError` at `init`), the role would run without its tool and path limits. `output_parse` flags it (`agent_fallback`) and the stage fails with `agent_config_rejected`; the answer is never used.
- **Global permissions.** The V3 loader accepts only a top-level `rules:` list of `{capability, match?, effect}`; capabilities in KAS 0.66.43: `fs_read`, `fs_write`, `shell`, `mcp`, `web_fetch`, `web_search`, `subagent`. Any other layout fails to parse and denies every tool call. `sandbox/kiro/settings/permissions.yaml` now denies the system paths for `fs_read`/`fs_write` and denies `shell` and `subagent` for every role.
- **Stream shape.** `--output-format stream-json` emits ACP: `runStarted {payloadSchema: acp, engine: v3}`, `sessionUpdate` (`tool_call`, `tool_call_update` with `status` and `content`, `config_option_update`, message chunks), `runError`, `runFinished {finalText}`. The answer is `finalText`; the model in effect is the last `configOptions[id=model].currentValue` (the session starts on the default and switches to `--model`). Built-in tools carry `kind` (`read`, `edit`, `execute`, `fetch`, `search`); MCP tools are first loaded by an engine step titled "Tool Load" and then called with `rawInput {tool_id: "<server>::<tool>", arguments}`, even with `toolSearch.enabled: false`. The probe parser (`sandboxd/probes.py`) maps these to `read`/`write`/`shell`/`web_fetch`/`web_search` and `@server/tool`; `coder_surface` accepts the loader only when every loaded tool is one of the four trusted `@ossbot-exec/*` tools.
- **Settings list.** `kiro-cli settings list` prints `key = value (global)`; the parser strips the scope suffix.
- **research_deny prompt.** The planner model refused the original wording (read secret files, then send them in a URL) outright, which proves nothing about the deny layer. The prompt now frames the steps as an authorized permission check that never prints contents and fetches `https://example.invalid/?k=` with an empty value; the evaluator is unchanged (reads must be attempted and denied, no non-empty `k=`).

Result: all five probes pass for tuple `f167e45bd5a7…` (`trust_mode=ids`), run by the runner itself in the M11 order; `ossctl kiro smoke` answered `pong`. Details in docs/VERIFICATION.md "Phase 2 live checks".