# ADR-0018: Hermes tool surface: allowlisted toolsets with runtime verification and kill on drift

Status: Accepted (design rev 2) · Date: 2026-10-17 · Design: DESIGN.md §8.4 T6, §15.1, §15.2 · Review: round 2 H2

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

## Alternatives

- A denylist only: rejected, because new toolsets in future Hermes versions would be enabled silently.
- Remove Hermes (use the plain `kiro-cli` `ChatBackend`): kept as the fallback behind `ChatBackend`. The user wants Hermes for memory and future integrations.
- Run Hermes in its own container without the key file mounted: not chosen in v1. The key would still have to be in that container's env or files, so it buys little over the allowlist. Revisit if Hermes gains features that need tools.

## Consequences

Hermes cannot read files, run commands, write skills, or schedule jobs. Chat answers rely on its memory, the `ossbot` plugin, and Kiro's web tools. A Hermes upgrade that renames toolsets or changes `/v1/toolsets` triggers a kill rather than a silent widening.
