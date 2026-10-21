# ADR-0012: Stack validation (compose/kind) is feature-flagged Phase 2
Status: Accepted (revised in design rev 1) · Date: 2026-10-17 · Design: DESIGN.md §10.7

## Context
The user wants real validation when cheap. Running a repo's compose file or a kind cluster needs the host Docker daemon; kind nodes are privileged containers, and compose files can request privileged mode, host mounts, or host networking. The Docker API filter (ADR-0001) deliberately does not allow compose's API surface. Sysbox (v0.7.1) could run Docker-in-Docker unprivileged, but nestybox/sysbox#991 (shim v3) suggests incompatibility with current containerd.

## Decision
- v1 validation tiers: static, unit, and runtime smoke (build + CLI `--help`/sample run) in sandboxes; upstream CI after submission.
- Phase 2 (`validation.stack.enabled: false` by default; per-repo `stack_validation: compose|kind`): compose files pass a deterministic linter on `docker compose config` output (no privileged, cap_add, devices, host namespaces, bind mounts, published ports); a root helper `ossbot-stack` (not sandboxd) runs them on the task's internal network with injected limits; kind runs from a generated config with Pod Security `baseline` and a non-admin kubeconfig. Enabled only by `ossctl stack enable` after `ossctl stack selftest`.

## Consequences
Some repos (operators, exporters) get weaker local validation in v1; the card states "relies on upstream CI".

## Implementation addendum (post-v1 open discovery, 2026-10-22)
The Phase-2 stack pool is now `10.213.224.0/19` (the upper half of the former stack /18); `10.213.192.0/19` is `pool_strict` for repos with `egress: strict` (ADR-0023).
