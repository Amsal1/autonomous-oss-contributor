# ADR-0024: Owner-controlled network for the coder's executor (amends I5, extends ADR-0023)
Status: Accepted (owner decision 2026-10-23) · Design: DESIGN.md §8.3, §9.2, §9.8, §10.4, §11.5, §13.6, §14.13.1; .ossbot-local/postv1/sandbox-network/design.md · Amends: ADR-0005 (I5), ADR-0023 · Related: ADR-0017, ADR-0003

## Context
The split coder (§10.4) runs every command of the coder in `agent-exec`, a credential-free container with `--network none`, so builds work only from the caches fetch prepared. Real repos keep needing things fetch did not foresee (a JDK, a plugin, a tool downloaded at test time); the kestra items c_D1D18SPWQ5 and c_D1D18TQJ75 failed `needs_network` with finished edits. The 2026-10-23 diagnosis fixed the sandbox gaps inside the old design and advised against network for agent-exec (its section (d)). On 2026-10-23 the owner decided otherwise: he wants to open the network when needed and rerun failures, "best to allow network and let it handle itself".

## Decision
- **Setting `implement_network: off | on_request | auto`** in the gatekeeper's control state, default `off` on a fresh DB, after a DB reset and on upgrade. Only the human changes it (Telegram `/egress`, picker + confirm tap; `ossctl egress implement-network`, TTY confirm); every change is audited and announced. It invalidates no approval: approvals bind content, config snapshot and dry-run, none of which it changes.
- **Effect.** `agent-exec` of a coder job gets the per-task network `ossbot-t-<cid>-exec` in the fetch squid pool, i.e. the fetch base list plus `web_public` behind the always-denied floors, when (a) the egress mode is `sandbox_open`, (b) the repo is not `egress: strict`, and (c) the setting is `auto`, or `on_request` and the item holds a human rerun grant. Otherwise `--network none`, as before.
- **Rerun with network.** A human two-tap on a `needs_network` or sandbox-gap failure drives the H-only edge `FAILED → PROPOSAL_APPROVED` (reason `network_rerun`), bounded by `policy.limits.network_reruns_max` (default 1, 0–3) and a 7-day window, refused for strict repos, outside `sandbox_open`, with the setting off, while paused or killed, held, or with a PR. It reuses the item's own consumed gate-1 approval and plan; gate 2 is unchanged. The grant is bound to the proposal version it was tapped for and is cleared on every move into RESCOPE_REQUIRED, PACKAGING, AWAITING_APPROVAL or a terminal state; a grant whose version differs from the item's current proposal version counts as no grant.
- **Eligibility default.** The button is offered for `needs_network` and for validation-loop failures (`implement_attempts`, `validation_stuck`) where the coder reported sandbox gaps; other failures are rerun with /work.
- **Fail closed, three layers.** The runner states `egress: mode | strict` on every coder job (sandboxd: 400 without it). sandboxd networks agent-exec only if its own read of the egress-mode flag is `sandbox_open` and the gatekeeper's per-run decision (audited) says `web`; any error means no network. The Docker filter allows agent-exec only `none` or its own cid's `-exec` network, binds agent-llm to its own `-llm` network, allows `-exec` networks only in the fetch pool, and allows `networks/connect` only for the proxy container, so no sandbox container can be attached to another network after create.
- **Guard.** Networked implement jobs are metered like fetch/validate (ADR-0023): a breach of `egress.anomaly` stops both containers of the run, discards the output and holds only that contribution until the human releases it.
- **I5 text.** Adds `agent-exec` under `implement_network` to the `sandbox_open` clause and states that strict repos' agent-exec never gets a network and that agent-llm and agent-exec are on separate per-task networks. The credential half is unchanged: no container both holds a credential and runs repo code; key-holding containers never mount an attacker-writable path and never get egress beyond their own profile's mode list.

## Alternatives
- Keep agent-exec offline and fix only the sandbox (the diagnosis' recommendation): done as Part 1 and still the default (`off`), but it cannot cover the long tail the owner expects.
- Give agent-exec only `pkg_*` registries: misses the hosts that caused the failures (JDK vendors, tool downloads) and still needs the same machinery.
- Rerun as a new contribution carrying the gate-1 approval: needs a gatekeeper-minted consumed approval nobody tapped, a second cid and duplicate-issue exceptions; rejected for a single human edge.
- Network for agent-llm instead: never; it holds the key.

## Consequences and residual risks
- While the coder works, repo- and model-driven commands can reach public hosts from the owner's IP; bounded by the floors, the per-job limits, the job timeout and the kill switch, not prevented.
- Web content can reach the coder's context through tool output (I8 keeps it data; the repo could already).
- New dependencies or downloaded code may enter the diff; the human sees it at gate 2, whose preview says when the coder had network.
- With `auto`, watcher fix rounds on live PRs are driven by third-party PR feedback, so that text can steer a networked coder. `FIX_ROUND_INSTRUCTION` forbids running tools, commands or URLs named in the feedback, I8 keeps the feedback data, and gate 2 still applies to every push; the `auto` confirm panel says it covers fix rounds on open PRs.
- A narrowing egress-mode switch takes effect on running jobs through the squid re-render; a setting change applies from the next coder job.
- FAILED is no longer absorbing for an item with a rerun left: it stays in `TERMINAL_STATES`, and every consumer (retention, prune, sandboxd destroy, rediscovery, backup) was checked; the 7-day rerun window is disjoint from the retention window.
- The safety probes do not cover agent-exec's network (they test Kiro's surface in agent-llm); unchanged.

## Verified facts
See the design's §2 (Gradle and Maven proxy/offline semantics, JDK and Maven pins, checked 2026-10-23) and the ADR-0005 implementation addendum of the same date.
