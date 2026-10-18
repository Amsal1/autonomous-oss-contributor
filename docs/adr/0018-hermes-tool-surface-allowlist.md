# ADR-0018: Hermes tool surface: allowlisted toolsets with runtime verification and kill on drift

Status: Accepted (amended in design rev 5 on 2026-10-18; design rev 2, amended rev 3) · Date: 2026-10-17 · Design: DESIGN.md §8.4 T6, §15.1, §15.2 · Review: round 2 H2; round 3 M8, M9, N7

## Context

Revision 1 planned to "disable terminal/code execution and file write; keep memory, todo, clarify, skills (read)" for Hermes. Hermes v0.22.2 docs were verified on 2026-10-17 (`toolsets-reference.md`, `cli-config.yaml.example`, configuration and API-server pages). They show three problems with that plan:

- The `hermes-api-server` platform "drops clarify, text_to_speech, computer_use, and the kanban tools. Keeps everything else". Terminal, file, code execution, delegation, cronjob, web and skills are all on by default.
- Toolsets bundle read and write: `file` = `patch, read_file, search_files, write_file`, and `skills` includes `skill_manage`. A read-only subset is not configurable.
- Nothing checked the live tool surface.

Meanwhile the `hermes` uid must be able to read `hermes.env` (Kiro key, API key) and has public-web egress via port 3141. One prompt injection in fetched web content could chain `read_file` with a web tool and exfiltrate the key.

## Decision

- **Allowlist, configured twice.** `platform_toolsets.api_server: [memory, todo, session_search, ossbot]` (the platform key name is [VERIFY]). `agent.disabled_toolsets` lists every other built-in toolset; it is documented to apply after platform config and to always remove what it lists. No MCP servers are configured. Hermes-side scheduled jobs are not used in v1.
- **Runtime enforcement, fail closed.** The gatekeeper calls the documented `GET /v1/toolsets` (bearer `API_SERVER_KEY`) at start, hourly, and after any Hermes reconnect. The allowed effective tools are `{memory, todo_list, session_search} ∪ ossbot_*`.
  - Any other tool, a 404, or a schema mismatch disables the chat relay and soft-kills (`hermes_tool_surface`) with a Telegram alert.
  - Connection errors only mark chat unavailable.
  - Doctor runs the same check.
- **Chat web research goes through Kiro, not Hermes.** The `ossbot-chat` Kiro agent has only `web_search` and `web_fetch`. Its revision-1 `read` tool is removed, so no model-reachable tool in the chat path reads local files or runs code.
- **Auth, stated plainly.** No Kiro auth option keeps the key away from code running as `hermes`: (a) puts it in the environment, (b) has the shim read the file. The control is the absence of file and exec tools, not the auth method. The residual risk is a code bug in Hermes, its plugins, or kiro-cli. It is accepted, with a dedicated revocable key, egress only via 3141, no docker, no sudo, and a read-only repo.

## Revision 3 amendments (design review round 3, M8/M9/N7)

- M8: Hermes Tool Search (`tools.tool_search.enabled`, default `auto`) would defer `ossbot_*`, `session_search` and `todo_list` behind `tool_search`/`tool_describe`/`tool_call`. The model-visible surface would then differ from the verified one. The rendered config sets it `off` ([VERIFY] parsing and api_server behaviour). The runtime check forbids the three bridge tools explicitly, and a fake returning them must cause a kill.
- N7: `/v1/toolsets` entries are parsed with `extra="ignore"` and require `name: str`, `enabled: bool` and `tools: list[str]`. Documented additive fields (`label`, `description`, `configured`) do not soft-kill the system.
- M9: the Kiro side of the chat path gets the same fail-closed treatment as the Hermes side. The adapter's mode and tool gate and the root-run Hermes-side probe both have to pass before the relay is enabled (ADR-0002).

## Alternatives

- A denylist only: rejected, because new toolsets in future Hermes versions would be enabled silently.
- Remove Hermes (use the plain `kiro-cli` `ChatBackend`): kept as the fallback behind `ChatBackend`. The user wants Hermes for memory and future integrations.
- Run Hermes in its own container without the key file mounted: not chosen in v1. The key would still have to be in that container's env or files, so it buys little over the allowlist. Revisit if Hermes gains features that need tools.

## Consequences

Hermes cannot read files, run commands, write skills, or schedule jobs. Chat answers rely on its memory, the `ossbot` plugin, and Kiro's web tools. A Hermes upgrade that renames toolsets or changes `/v1/toolsets` triggers a kill rather than a silent widening.

## Revision 5 amendment (consolidation, 2026-10-18)

Merged from CR-002 incl. Amendment A (DESIGN §15.5, AC 25).

- The allowlist becomes per tier: `locked` = `memory`, `todo_list`, `session_search`, `ossbot_*`; `research` adds Hermes-native web tools only if they are proven to route through proxy 3141; `self_improve` (default) adds `skill_manage`, `skill_view`, `skills_list`. `terminal`, `code_execution`, `file`, `browser`, `delegation`, `cronjob` and the Tool Search bridge are forbidden in every tier.
- The gatekeeper's `/v1/toolsets` check uses the active tier's allowlist; drift is still a soft kill `hermes_tool_surface`. A tier switch re-renders the config as root, restarts Hermes, re-runs the Hermes-side Kiro probe and the surface check before the relay reopens, and invalidates pending skill approvals.
- Skills cannot widen the surface: the rendered config, not a skill, defines toolsets, and a skill becomes active only after its two-tap approval card.
## Implementation addendum (FEAT-005 item 23a, 2026-10-18): tier control plane
- `config/policy.yaml` `hermes.capability_tier_default: self_improve` (`HermesPolicy`, strict). The active tier is the control flag `/var/lib/ossbot/control/hermes-tier` (`{tier, actor, ts}`), read by `control_flags.read_hermes_tier`: absent = the policy default; unparseable, oversize, a symlink or an unknown tier = `locked`.
- `gatekeeper/chat/hermes_surface.py`: `TIER_TOOLSETS` / `allowed_toolsets(tier)` (locked: `memory`, `todo`, `session_search`, `ossbot`; research: + `web` only once `HERMES_WEB_VIA_PROXY` is set by PLAN item 33 after the §23 [VERIFY], until then equal to locked; self_improve: + `skills`) and `ALWAYS_DISABLED` (`terminal`, `code_execution`, `file`, `browser`, `delegation`, `cronjob`), off in every tier.
- `ControlService.set_hermes_tier` (operator `POST /v1/control/hermes-tier`, `ossctl hermes tier <tier>` with a TTY confirm) writes the flag, audits `hermes_tier_changed`, FYIs and invalidates every open skill approval with `tier_changed` in the same transaction. Nothing applies a tier to Hermes yet: the config render, restart, probe and surface check are PLAN item 36 (FEAT-007). Import-linter: `ossbot.gatekeeper.chat` never imports `submit`/`github_writer`.
