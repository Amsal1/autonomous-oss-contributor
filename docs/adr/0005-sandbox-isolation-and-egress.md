# ADR-0005: Sandbox isolation via per-stage profiles, a split coder, per-task internal networks, and a squid allowlist
Status: Accepted (revised in design rev 1, rev 2 and rev 3) · Date: 2026-10-17 · Design: DESIGN.md §10, §11

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

## Revision 2 amendments (design review round 2)
- H1: the coder's built-in `read`/`write` and research's read-only task-volume mount are replaced. Key-holding containers never mount an attacker-writable path. Research reads a symlink-free snapshot volume, and `agent-llm` has no `/work` mount and uses `@ossbot-exec/*` file tools. Denies are extended to `/proc`, `/sys`, `/dev`, `/run`. See ADR-0017.
- M2: live probes gate Kiro jobs per (CLI, image, agent config, settings, trust mode) tuple (ADR-0017).
- M3/M4: stage I/O at image-rooted `/io/input` (read-only, filled by a `stage-in` container) and `/io/output`; complete Docker filter create allowlist.
- M6/M7: firewall split into `ossbot-firewall` (before Docker) and `ossbot-firewall-docker` (`BindsTo`/`PartOf` Docker), own chains, `ExecStartPre --check` on every service (fail closed); proxy DNS allowed to the WSL resolver only.
- **Addendum slot (implementation):** result of "does Kiro `read` resolve symlinks before matching `deniedPaths`?" (DESIGN §23 5a). Not load-bearing after H1.

## Revision 3 amendments (design review round 3, M2/M11/N5)

- M2: a fresh named volume takes its root owner and mode from the image directory (copy-up). So the writable mountpoints `/work`, `/work/repo`, `/io/output`, `/run/exec`, `/snap` and `/home/agent` are created `10001:10001 0700`, and `/io/input` and `/in` stay `root 0755`. The read-only rootfs, not ownership, is what prevents mountpoint swaps.
- M2: Docker mounts every tmpfs `noexec` unless `exec` is given. `/tmp` gets `exec` only in the code-running profiles (fetch, validate, agent-exec, package), and the Docker filter enforces that profile set. LLM profiles keep `noexec`. If kiro-cli is proven to need exec, the only allowed change is `exec` on `/tmp` for the LLM profiles, recorded here with probe evidence. Sandbox test S-perm-1.
- M11: new `catalog` profile for `list_models` only: `sandbox-llm`, `repo_mode none`, no inputs, network from the `agent-llm` pool (`kiro_core` only). It is exempt from the probe gate (ADR-0017).
- N5: the Docker filter refuses container start, not only create, while killed or paused.
- **Addendum slot (implementation):** whether kiro-cli needs exec under `/tmp` or `$HOME` (DESIGN §23 5), and the Docker 29 copy-up ownership result (§23 14).

## Consequences
Two containers per implement run and an MCP relay to maintain. The coder has no web tools in v1 (planner does the research). **Verify:** exact V3 tool ids and MCP tool naming, workspace config discovery rules (S-ws-1), Kiro host set via learn mode, whether web tools are server-side.
