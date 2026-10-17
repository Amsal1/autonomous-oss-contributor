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
