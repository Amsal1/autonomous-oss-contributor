# ADR-0002: Agent runtime is Hermes (narrowed role) with a vendored kiro-acp provider, V3 ACP primary
Status: Accepted (revised in design rev 1) · Date: 2026-10-17 · Design: DESIGN.md §15

## Context
The user wants a conversational agent on Telegram with memory and room for future integrations, using unlimited Kiro access through the official CLI only. MITM model routers (9router, OmniRoute) are rejected for ToS reasons. User decision (2026-10-17): Hermes uses Kiro **V3 ACP** as primary.

Live doc check (kiro.dev/docs/cli/v3/acp-migration, 2026-10-17): launch `kiro-cli acp --agent-engine=v3`; v2-only flags `--agent --model --effort --trust-all-tools --trust-tools` rejected; capabilities negotiated via `initialize` and `agentCapabilities._meta.kiro.extensionMethods`; `session/new` accepts `_meta.kiro.{modeId, modelId, effortLevel}`; model via `session/set_config_option configId:"model"` (no `session/set_model`); default auth = CLI login, `--auth-method=client` → `_kiro/auth/getAccessToken`; permissions via standard `options[]`. The page does not mention `KIRO_API_KEY`.

## Decision
- Hermes Agent pinned to `v0.22.2`, Unix user `hermes`, API-server platform only (the Telegram bot belongs to the gatekeeper, ADR-0004).
- Provider: fork of `tobenwarrior/hermes-kiro-provider@02c4dd06585436751145961658a22b773f23a0ef` vendored at `vendor/hermes-kiro-provider/` (`UPSTREAM_COMMIT`, own history), with a separate V3 adapter beside the original V2 code.
- V3 adapter: launch without v2 flags, cwd outside any repo, handlers registered before `session/new`, capability-gated optional calls, live model/effort switch from `config/models.yaml` via `set_config_option`, turn completion from the prompt response only, `session/load` on reconnect without resending prompts.
- Permission requests are always answered by deterministic code with a reject option (or `cancelled`), never with consent metadata. Kiro-side deny rules for `hermes` stay as defence in depth.
- Auth order, each proven by live probe: (a) `KIRO_API_KEY` with default auth; (b) `--auth-method=client`, shim answers `getAccessToken` from `hermes.env`, token never logged; (c) device-flow login as `hermes`.
- Fallback 1: V2 ACP via the original plugin path. Fallback 2: per-turn `kiro-cli chat --v3 --no-interactive` provider. Last resort: replace Hermes behind the gatekeeper's `ChatBackend`.
- Upstreaming the V3 adapter is the first candidate contribution, via `ossctl contribution import` and normal approval.

## Alternatives
- OpenClaw: rejected in the brief; trust concerns would be the same, which is why neither is a safety boundary.
- No framework: loses memory/skills/future integrations the user asked for.
- Third-party "Kiro gateway" bridges: rejected (unvetted, router-like).

## Consequences
We own a small fork. The plugin has one maintainer and no users; contract tests against `tests/fakes/fake_acp_v3.py` and a doctor round-trip guard upgrades. **Build-time probe (to append here at implementation):** real `kiro-cli acp --agent-engine=v3` initialize + session/new transcript (redacted), advertised extension methods, mode/model options, and which auth option worked.
