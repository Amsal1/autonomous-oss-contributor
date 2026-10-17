# ADR-0005: Sandbox isolation via per-stage profiles, a split coder, per-task internal networks, and a squid allowlist
Status: Accepted (revised in design rev 1) · Date: 2026-10-17 · Design: DESIGN.md §10, §11

## Context
The coder must run hostile repo code (tests, install scripts) and kiro-cli needs a Kiro key. Hostile code at the same UID can read the key from `/proc/<pid>/environ`, and "Kiro hosts give an attacker no readable channel" is unverified (server-side web tools, remote MCP, cloud sessions). A hostile repo can also ship `.kiro/` workspace config (MCP servers, hooks). IP allowlists are brittle for CDN-fronted hosts.

## Decision
- Stages run in throwaway containers (UID 10001, `--cap-drop ALL`, `no-new-privileges`, read-only rootfs, default seccomp, limits, per-task volume, no host mounts, `--pull never`, `--dns 127.0.0.1`), created only through the Docker API filter (ADR-0001).
- **Invariant: no container both holds a credential and executes repo code.**
  - `fetch`/`validate`/`package`: run repo code or git on repo data; no secrets.
  - `research` (vetter, planner, reviewer, summarizer): Kiro key + public web (summarizer: no web), tools `read`/`web_*` only, repo read-only.
  - Coder = `agent-llm` (key, kiro-cli, tools `read`, `write` on `/work/repo/**`, `@ossbot-exec/run`; no shell; Kiro-only egress) + `agent-exec` (no key, `--network none`, deterministic `ossbot-execd` running commands with a fixed env). kiro-cli is not even installed in code-running images.
  - All kiro-cli processes use a tmpfs HOME and an empty tmpfs cwd, never the repo, so repo-local `.kiro/` config is not loaded.
- Single-container coder mode exists only as an audited downgrade, refused unless an ADR addendum proves no attacker-directed egress via Kiro features and env scrubbing by `kiro-cli --sandbox`.
- sandboxd scans every output, log, and bundle blob for the literal key and its encodings before publishing (fail + pause); the gatekeeper hash-checks token-like strings against the key hashes.
- Each task/profile has its own `--internal` network with a persisted /28; the proxy (10.212.0.2 on `ossbot-egress`) is attached with alias `proxy`; squid selects the profile by source subnet and matches CONNECT host names (no TLS interception). Host users get one proxy port each (runner 3140, Hermes 3141, gatekeeper 3150 limited to Telegram + GitHub) enforced by owner-match iptables; no direct 443/53 for any service user.

## Alternatives
- Key and hostile code in one container with Kiro-only egress (v1): rejected (review H3).
- TLS-intercepting proxy to allow fetch but not push: rejected for complexity and CA distribution.
- Kiro local command sandbox as the primary control: unverified inside containers; only relevant to the downgrade mode.

## Consequences
Two containers per implement run and an MCP relay to maintain. The coder has no web tools in v1 (planner does the research). **Verify:** exact V3 tool ids and MCP tool naming, workspace config discovery rules (S-ws-1), Kiro host set via learn mode, whether web tools are server-side.
