# ADR-0017: Key-holding containers never mount attacker-writable paths; coder file I/O through execd; safety-probe gate

Status: Accepted (design rev 2) · Date: 2026-10-17 · Design: DESIGN.md §8.3 I5, §10.2, §10.4, §10.9 · Review: round 2 H1, M2, M3, M4

## Context

Revision 1 kept the Kiro key out of every container that runs repo code (ADR-0005). But both key-holding containers still saw attacker-controlled trees:

- `research` mounted the task volume read-only.
- `agent-llm` mounted it read-write, alongside `agent-exec`.

A committed or planted symlink (`docs/notes -> /proc/self/environ`, or `/proc/1/environ`, because `--init` gives docker-init the same env) turns a Kiro `read` into a key read. Research roles also have public-web egress for exfiltration. The only defence was path-based `deniedPaths`, and whether Kiro canonicalizes paths before matching is unverified. The live probes that should have caught this were advisory, and `--trust-all-tools` was a silent fallback.

Revision 1 also wrote stage inputs and outputs under `/work` on attacker-writable or read-only volumes, which either fails (archive PUT into a read-only mount) or can be pre-planted.

## Decision

- Invariant I5: no key-holding container mounts an attacker-writable path. The design does not rely on how Kiro matches paths.
  - `research` sees `/work/repo` only as a per-run read-only snapshot volume (`ossbot-ro-<run_id>`). A deterministic `--network none` job builds it from `git ls-files` with `rsync --no-links --no-specials --no-devices`. Symlinks become `*.symlink.txt` text stubs. A self-check and a sandboxd post-check run on the result. Any link still followed during snapshotting resolves in a key-free container.
  - `agent-llm` has no `/work` mount and no built-in file tools. The coder's only tools are `@ossbot-exec/{read_file,write_file,list_dir,run}`, served by `ossbot-execd` in `agent-exec`, which has no key and `--network none`. execd confines paths with `realpath`, `O_NOFOLLOW`, and a `/proc/self/fd` re-check, and refuses writes into `.git/` and `.kiro/`.
  - The exec socket directory is the only shared read-write mount. Kiro never reads it, and the relay only `connect()`s to it after an `lstat` check.
- Every job uses fixed image-rooted mountpoints: `/io/input` (read-only, filled by a created-never-started `stage-in` container) and `/io/output` (per-run). Nothing is nested inside a volume.
- Deny lists also cover `/proc`, `/sys`, `/dev`, `/run`, `/etc` and `~/.kiro` as a second layer. `cli.json` turns off knowledge, code intelligence, delegate, subagent and workflows.
- **Safety-probe gate.** sandboxd refuses Kiro jobs (423) until five live probes pass for the exact tuple `(kiro_cli_version, sandbox_llm_digest, agent_configs_sha256, settings_sha256, trust_mode)`. The probes are `research_deny`, `symlink`, `coder_surface`, `settings` and `workspace`. `--trust-all-tools` is an explicit snapshot setting and needs its own pass. Upgrades re-run the probes before switching.
- The Docker filter enforces all of this structurally. It has a complete create allowlist, profile-specific mount rules (`agent-llm` gets no task or snapshot volume; `research` gets no task volume), and accepts archive PUT only for `stage-in` containers at `/in`.

## Alternatives

- Rely on Kiro deny rules plus a probe. Rejected: it is unverified behaviour, and a probe cannot prove the absence of every bypass.
- Strip symlinks in place on the task volume. Rejected: hostile code can recreate them at any time, including while the coder runs.
- `volume-subpath` mounts. Rejected: symlink handling in subpaths adds new trust in Docker internals for no gain.

## Consequences

Each research stage costs one extra short container, plus a snapshot copy capped at 2 GiB. The coder's file I/O runs over a Unix socket, so Kiro's native edit UX (diff rendering) is unavailable; this is acceptable in headless mode. **Verify** (DESIGN §23 5, 5a, 5b, 14): V3 MCP tool id format; Kiro symlink handling (recorded here as an addendum, not load-bearing); org governance allowing the local stdio MCP server; archive PUT into a created container's volume on Docker 29.
