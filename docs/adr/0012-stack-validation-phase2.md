# ADR-0012: Stack validation (compose/kind) is feature-flagged Phase 2

Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §10.7

## Context
The user wants real validation when cheap. Running a repo's compose file or a kind cluster needs the host Docker daemon; kind nodes are privileged containers, and compose files can request privileged mode, host mounts, or host networking. Sysbox (v0.7.1) could run Docker-in-Docker unprivileged, but an open issue (nestybox/sysbox#991, shim v3) suggests incompatibility with current containerd.

## Decision
- v1 validation tiers: static + unit tests in sandboxes, upstream CI after submission.
- Phase 2 (`validation.stack.enabled: false` by default; per-repo `stack_validation: compose|kind`): compose files pass a deterministic policy linter on `docker compose config` output (no privileged, cap_add, devices, host namespaces, bind mounts, published ports) and run on the task's internal network with injected limits; kind runs from a sandboxd-generated config with Pod Security `baseline` enforced and a non-admin kubeconfig. Enabled only by `ossctl stack enable` after `ossctl stack selftest` passes.

## Consequences
Some repos (operators, exporters) get weaker local validation in v1; the card states "relies on upstream CI".
