# ADR-0003: A deterministic gatekeeper is the only GitHub write-token holder and policy core
Status: Accepted (revised in design rev 1 and rev 3) · Date: 2026-10-17 · Design: DESIGN.md §8, §9.2, §9.4, §12

## Context
LLM sessions read hostile text (issues, comments, repo files, web pages). If any LLM-adjacent process can perform GitHub writes or decide approvals, prompt injection can publish under `Amsal1`.

## Decision
A small Python service (`ossbot-gatekeeper`, Unix user `gatekeeper`) is the trusted computing base together with the root helpers (dockerfilter, enforcer, watchdog):
- only non-root user that can read the classic PAT; the only module calling write APIs is `gatekeeper/submit.py` (import-linter enforced);
- owns control state (kill/pause/dry-run, dry-run default ON), the config snapshot, the contribution state machine with **actor-owned edges** (worker / gatekeeper_internal / human; approval and submit edges are never worker edges), approvals, rate limits, policy checks, and the hash-chained audit log;
- derives every artifact the human reviews from its own per-contribution mirror (ADR-0014);
- exposes a Unix-socket API with roles from `SO_PEERCRED` (operator, worker, sandbox, requester). There is no approve endpoint; approval exists only as a Telegram two-tap flow.
- Asymmetric control: any role may request stop; only the human may request go. Every local operator go action is announced on Telegram.

## Revision 3 amendments (design review round 3, M4/M5/M6)
- M4: the state machine owns "an open PR is never orphaned". While `pr_number` is set, every requested move to `FAILED` or `ABANDONED` is redirected to `WATCHING` (`fix_round_failed:<class>`) and the task volume is kept. The only terminal exits are `MERGED`/`CLOSED` (worker, observed on GitHub) and `ABANDONED` through the human "Stop watching" button. `branch_moved` disables further automated fixes for that PR.
- M5: submission is checkpointed against kill and pause before each write. An interrupted submission ends `SUBMIT_FAILED` and needs a fresh approval. `executing` actions are reconciled at start by reads only, and writes are never resumed automatically.
- M6: one non-terminal contribution per issue, enforced by a partial unique index. Re-discovery is decided by deterministic rules in the gatekeeper's insert path: never after reject, merge or close; after a dry run only once dry-run is off; after failure or expiry only after a 7-day cooldown; after a repo-policy skip only once the human unlocks the repo.
## Alternatives
- Let Hermes hold the token with "careful prompts": rejected (prompting is not a control).
- Runner and gatekeeper in one process: rejected (runner parses large untrusted LLM output; keep the TCB small).

## Consequences
More moving parts (runner ↔ gatekeeper API). Every public action is re-validated at execution time, so stale approvals fail closed.
