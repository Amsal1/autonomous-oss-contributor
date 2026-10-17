# ADR-0002: Agent runtime is Hermes (narrowed role) with the kiro-acp provider

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §15

## Context
The user wants a conversational agent on Telegram with memory and room for future integrations, using unlimited Kiro access through the official CLI only. Candidates: Hermes Agent (NousResearch, v0.22.2, very active), OpenClaw, or no framework. MITM model routers (9router, OmniRoute) are rejected for ToS reasons.

## Decision
- Run Hermes Agent pinned to `v0.22.2` as Unix user `hermes`, API-server platform only (the Telegram bot belongs to the gatekeeper, ADR-0004).
- Model provider: `tobenwarrior/hermes-kiro-provider` (kiro-acp) vendored at commit `02c4dd06585436751145961658a22b773f23a0ef`, spawning `kiro-cli acp` with a tool-less `ossbot-chat` Kiro agent and global deny rules for shell/write.
- Hermes is the conversational interface, not the scheduler of the pipeline and not a safety boundary. The pipeline's LLM roles call `kiro-cli chat --v3 --no-interactive` directly inside sandboxes.
- Fallbacks in order: vendored V3-ACP fork of the provider; per-turn `kiro-cli chat --no-interactive` backend; replace Hermes behind the gatekeeper's `ChatBackend` interface.

## Alternatives
- OpenClaw: rejected in the brief. Hermes has a larger, very active project (weekly releases in Oct 2026) and a documented `external_process` (ACP) provider seam that kiro-acp uses; trust concerns would be the same either way, which is why neither is a safety boundary here.
- No framework: simplest, but loses memory/skills/future integrations the user asked for.
- Third-party "Kiro gateway" bridges: rejected (unvetted, router-like).

## Consequences
The kiro-acp plugin has a single maintainer and was verified only against kiro-cli 2.22 V2 ACP; CLI 3.0 (end of Oct 2026) may change ACP defaults. Doctor runs a round-trip after every upgrade. **Verify:** ACP with `KIRO_API_KEY`, `acp --agent-engine v2`, Hermes version pinning and toolset names.
