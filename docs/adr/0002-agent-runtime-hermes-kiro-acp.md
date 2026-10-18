# ADR-0002: Agent runtime is Hermes (OSS-side role narrowed; general multimodal assistant since rev 5) with a vendored kiro-acp provider, V3 ACP primary
Status: Accepted (amended in design rev 5 on 2026-10-18; revised in design rev 1, rev 2, rev 3 and rev 4) · Date: 2026-10-17 · Design: DESIGN.md §15

## Context
The user wants a conversational agent on Telegram with memory and room for future integrations, using unlimited Kiro access through the official CLI only. MITM model routers (9router, OmniRoute) are rejected for ToS reasons. User decision (2026-10-17): Hermes uses Kiro **V3 ACP** as primary.

Live doc check (kiro.dev/docs/cli/v3/acp-migration, 2026-10-17): launch `kiro-cli acp --agent-engine=v3`; v2-only flags `--agent --model --effort --trust-all-tools --trust-tools` rejected; capabilities negotiated via `initialize` and `agentCapabilities._meta.kiro.extensionMethods`; `session/new` accepts `_meta.kiro.{modeId, modelId, effortLevel}`; model via `session/set_config_option configId:"model"` (no `session/set_model`); default auth = CLI login, `--auth-method=client` → `_kiro/auth/getAccessToken`; permissions via standard `options[]`. The page does not mention `KIRO_API_KEY`.

## Decision
- Hermes Agent pinned to `v0.22.2`, Unix user `hermes`, API-server platform only (the Telegram bot belongs to the gatekeeper, ADR-0004).
- Provider: fork of `tobenwarrior/hermes-kiro-provider@02c4dd06585436751145961658a22b773f23a0ef` vendored at `vendor/hermes-kiro-provider/` (`UPSTREAM_COMMIT`, own history), with a separate V3 adapter beside the original V2 code.
- V3 adapter: launch without v2 flags, cwd outside any repo, handlers registered before `session/new`, capability-gated optional calls, live model/effort switch from `config/models.yaml` via `set_config_option`, turn completion from the prompt response only, `session/load` on reconnect without resending prompts.
- Permission requests are always answered by deterministic code with a reject option (or `cancelled`), never with consent metadata. Kiro-side deny rules for `hermes` stay as defence in depth.
- (rev 2) Hermes's own tool surface is an allowlist (memory, todo, session_search, ossbot), enforced at runtime by the gatekeeper via `GET /v1/toolsets` with a kill on drift (ADR-0018). The `ossbot-chat` Kiro agent has only `web_search`/`web_fetch`. `~hermes/.kiro/settings/cli.json` disables knowledge and code intelligence. No auth option keeps the key away from the `hermes` uid; the control is that no model-reachable tool reads files or runs code.
- Auth order, each proven by live probe: (a) `KIRO_API_KEY` with default auth; (b) `--auth-method=client`, shim answers `getAccessToken` from `hermes.env`, token never logged; (c) device-flow login as `hermes`.
- Fallback 1: V2 ACP via the original plugin path. Fallback 2: per-turn `kiro-cli chat --v3 --no-interactive` provider. Last resort: replace Hermes behind the gatekeeper's `ChatBackend`.
- Upstreaming the V3 adapter is the first candidate contribution, via `ossctl contribution import` and normal approval.

## Revision 3 amendments (design review round 3, M9/M10)
- M9: the V3 adapter fails closed on its session surface. After `session/new` and `session/load`, the current mode must be `ossbot-chat`. Where V3 advertises a tool list, it must be ⊆ {`web_search`, `web_fetch`}. Otherwise the adapter refuses the turn and its `on_surface_violation` hook (set by ossbot to a gatekeeper UDS kill) soft-kills with `hermes_kiro_surface`.
- M9: a root helper, `ossbot-hermes-probe`, acts as the ACP client with the adapter's own launch and session parameters (code origin and key refined in rev 4, R4-M2 below). It runs a `research_deny`-style probe (read the env, `hermes.env` and `cli.json`, run `id`, `web_fetch` the contents) and evaluates the stream deterministically. The result is keyed by `(kiro_cli_version, hermes_agent_sha256, permissions_sha256, settings_sha256)`. The gatekeeper enables the chat relay only while a pass younger than 2 h exists. Neither Hermes nor the gatekeeper can forge the result.
- M10: Hermes is installed from `git clone --depth 1 --branch v0.22.2`. The annotated tag object and the peeled commit are verified against `versions.lock.yaml`, then `uv sync --frozen --no-dev` runs as `hermes`. The website installer is never used, and `setup-hermes.sh` is not executed (its non-dependency steps are reproduced in `provision.sh`). `ossctl hermes upgrade` takes the same path.
- **Addendum slot (implementation):** the peeled commit, the list of reproduced `setup-hermes.sh` steps, and the observed `modes.currentModeId` and tool-list fields for `modeId: "ossbot-chat"`.

## Revision 4 amendments (design review round 4, R4-M2/R4-N4)
- R4-M2: no hermes-writable byte ever runs as root.
  - The root probe runs `/opt/ossbot/app/.venv/bin/python -I -m ossbot.ctl.hermes_probe` with cwd `/`.
  - It imports `kiro_acp.v3_core` (stdlib-only launch, session and surface logic, no Hermes imports) only from the root-owned copy `/opt/ossbot/app/vendor/hermes-kiro-provider/` (root:root, 0755/0644). It never imports from, or adds to `sys.path`, anything under `/home/hermes`.
  - It hashes the deployed `~hermes/.hermes/plugins/model-providers/kiro-acp/**` (regular files, `O_NOFOLLOW`, sorted paths) against the root copy. A mismatch or a link fails the probe with `adapter_drift`.
  - The probe key becomes `(kiro_cli_version, hermes_agent_sha256, permissions_sha256, settings_sha256, adapter_sha256)`.
  - Hermes-owned executables run only via `runuser -u hermes --`, and hermes-owned files are only hashed, never executed or acted on as config.
- R4-N4: the Hermes install clones and verifies in root-owned `/var/lib/ossbot/staging/hermes-<tag>`.
  - It runs `chown -R --no-dereference`, then swaps with `mv -T $D $D.prev; mv -T staging $D`. `uv sync --frozen` then runs as `hermes` at the final path, because venvs are not relocatable, and a failure rolls back to `$D.prev`.
  - Before touching `~/.hermes`, the step stops Hermes, kills every `hermes` process, and refuses if any path involved is a symlink.
## Alternatives
- OpenClaw: rejected in the brief; trust concerns would be the same, which is why neither is a safety boundary.
- No framework: loses memory/skills/future integrations the user asked for.
- Third-party "Kiro gateway" bridges: rejected (unvetted, router-like).

## Consequences
We own a small fork. The plugin has one maintainer and no users; contract tests against `tests/fakes/fake_acp_v3.py` and a doctor round-trip guard upgrades. **Build-time probe (to append here at implementation):** real `kiro-cli acp --agent-engine=v3` initialize + session/new transcript (redacted), advertised extension methods, mode/model options, and which auth option worked.

## Revision 5 amendment (consolidation, 2026-10-18)

Merged from CR-002 incl. Amendment A (DESIGN §15.5, §15.6) and the user's chat decisions (DESIGN §25).

- Hermes's role widens from "narrowed chat front-end" to the user's **general-purpose multimodal assistant**: every non-command Telegram update (text, photos, albums, documents, voice, locations, quoted replies) is relayed to it; answers stream with sources; features ship as skills or `ossbot` plugin tools with per-feature SQLite stores; a reverse-image-search provider is a documented hook only until the user picks one. The gatekeeper still owns the bot, the deterministic commands and every public GitHub write.
- Capability tiers `locked` / `research` / `self_improve` (default `self_improve`) replace the single allowlist (ADR-0018 amendment). Self-written skills are staged inactive and activated only through a two-tap Telegram skill card; root copies the bound bytes into a read-only active dir. `terminal`, `code_execution`, `file`, `browser`, `delegation` and `cronjob` stay off in every tier; code runs only as gatekeeper-scheduled sandbox jobs.
- Images go to the model as ACP image content blocks when V3 advertises `promptCapabilities.image`, on the new `vision` role (ADR-0009 amendment); attachments are downloaded by the gatekeeper with a size cap, a type allowlist and metadata stripping without Pillow (ADR-0020).
- Unchanged and re-affirmed: the vendored fork of `tobenwarrior/hermes-kiro-provider` on Kiro **V3 ACP** is primary, V2 ACP is the fallback, and the fork's V3 adapter is queued as the first upstream contribution through `ossctl contribution import`; pipeline roles run `kiro-cli chat --v3 --no-interactive`.
