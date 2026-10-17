# ADR-0005: Sandbox isolation via per-stage profiles, per-task internal networks, and a squid allowlist

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §10, §11

## Context
The coder must run hostile repo code (tests, install scripts) and needs a Kiro credential to run at all. IP allowlists are brittle for CDN-fronted hosts. Kiro env cannot be reliably hidden from child processes.

## Decision
- Stages run in throwaway containers (non-root UID 10001, `--cap-drop ALL`, `no-new-privileges`, read-only rootfs, cpu/mem/pids limits, per-task volume, no host mounts, `--pull never`).
- Profiles split capabilities so no container has both secrets and an exfiltration path:
  - `fetch`/`validate`: GitHub read + package registries, no secrets.
  - `research` (vetter, planner, reviewer, summarizer): Kiro key + public web, but no shell/write tools and fs_read denies on procfs and Kiro dirs.
  - `agent` (coder): Kiro key + shell, egress to Kiro hosts only; builds/tests offline from prefetched caches.
  - `package`: `--network none`, deterministic commit/bundle.
- Each task/profile gets its own `--internal` Docker network; the squid proxy container is attached with alias `proxy`; squid picks the profile by source subnet and matches CONNECT host names (no TLS interception). iptables `DOCKER-USER`/`INPUT` rules drop anything else; host users are owner-matched to the proxy.
- Outbound text is scanned for token patterns (Kiro `ksk_`, GitHub, Telegram) before anything leaves.

## Alternatives
- One sandbox with a broad allowlist: rejected (key exfiltration via attacker-supplied credentials to github.com or registries).
- TLS-intercepting proxy to allow git fetch but not push: rejected for complexity and CA distribution into every toolchain.
- Kiro local command sandbox as primary control: unverified inside containers; used only as an optional extra layer.

## Consequences
Coder cannot fetch new dependencies directly; it requests them and fetch re-runs (bounded). **Verify:** exact Kiro hosts via `ossctl egress learn`; whether Kiro web tools are server-side.
