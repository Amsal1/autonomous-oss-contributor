# ADR-0011: Kiro CLI version pinning and explicit V3 engine for headless runs

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §15.3

## Context
Kiro CLI 2.x ships two harnesses (V2 classic, V3). The release manifest (`desktop-release.q.us-east-1.amazonaws.com/latest/manifest.json`) listed 2.32.0 on 2026-10-17 with per-package sha256. CLI 3.0 will make V3 default at the end of October 2026. V3 adds permissions.yaml, stream-json, `--sandbox`, exit code 4 for missing agents; V3 ACP drops launch flags used by the kiro-acp plugin.

## Decision
- Install the headless tarball for a pinned version, verify sha256 from the manifest, record in `config/versions.lock.yaml`; upgrade only via `ossctl kiro upgrade` (new image tag + doctor smoke run).
- Always pass `--v3` for pipeline headless runs. Hermes's ACP subprocess pins the V2 ACP server while the plugin requires it, with a V3 provider fork as fallback.
- Use `--output-format stream-json`, `--agent ossbot-<role>`, `--model`, `--effort`, `--trust-all-tools` (tool limits come from agent definitions + permissions deny rules; the container is the boundary).

## Consequences
Re-verify flags and engine behaviour when CLI 3.0 ships.
