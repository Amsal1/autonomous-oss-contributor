# ADR-0003: A deterministic gatekeeper is the only GitHub write-token holder and policy core
Status: Accepted (revised in design rev 1, rev 3 and rev 4) · Date: 2026-10-17 · Design: DESIGN.md §8, §9.2, §9.4, §12

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

## Revision 4 amendments (design review round 4, R4-N2/R4-N3)
- R4-N2: each submission checkpoint requires `running` and also `control_state.dry_run == snapshot.dry_run`. A dry-run toggle that lands mid-submission stops the next write, the same way a pause does: `interrupted`, `SUBMIT_FAILED`, "mode changed during submission", and a fresh approval is needed.
- R4-N3: the closed API gains `POST /v1/models/catalog` (worker) for the runner's catalog upsert. It takes 1-200 entries, validates ids with the `--force` regex, has no `forced` field, and never changes `forced`.

## Alternatives
- Let Hermes hold the token with "careful prompts": rejected (prompting is not a control).
- Runner and gatekeeper in one process: rejected (runner parses large untrusted LLM output; keep the TCB small).

## Consequences
More moving parts (runner ↔ gatekeeper API). Every public action is re-validated at execution time, so stale approvals fail closed.

## Implementation addendum (FEAT-001, 2026-10-17)
- Peer credentials: uvicorn 0.54 keeps the ASGI app per connection (`H11Protocol.app`). `ossbot.gatekeeper.authz.PeerCredH11Protocol` reads `SO_PEERCRED` once in `connection_made` and wraps that connection's app so every request scope carries `scope["ossbot.peer"]`. Roles map from the peer uid only. The socket is created by the gatekeeper itself with umask 0117 (0660 from the start), group `ossapi`, and handed to `uvicorn.Server.run(sockets=[...])`.
- All endpoints are `async def`, so every SQLite transaction runs on the event-loop thread and transactions never interleave on the shared connection.
- FastAPI 0.143 stores included routers lazily (`_IncludedRouter.original_router`). The route-table test (no approve route, role matrix of §12.2) walks those wrappers.
- Kill/pause on transitions: entry into every working state and into `SUBMITTING` is refused while paused or killed (423). Moves that stop work are allowed: the live-PR redirect into `WATCHING`, any move into `SUBMIT_FAILED`, and `WATCHING` reached from a gate state. Restart reconciliation (M5) needs that last case.
- Resume detection: the enforcer removes the flags; `control.reconcile()` runs at start and every 10 s. It moves `control_state` to `running` only when all flags are absent and either this process wrote `resume-request` or new `resume.log` lines (the offline `ossbot-resume` path, N4) were found in the same pass. Otherwise N12 applies: DB killed without a flag stays killed with an alert, and DB paused without a flag rewrites the flag. `resume.log` is read on every reconcile, not only at start, because a soft-killed gatekeeper keeps running across an offline resume. A partial last line is not consumed, and a file shorter than the stored offset is read from the start.
- The binding for `open_pr`/`push_update` re-derives the bundle, patch and commit-message hashes through an `ArtifactSource` interface. Until the FEAT-002 git TCB provides it, the default `NoArtifacts` raises, so PR-kind approvals fail closed.
- `POST /v1/discovery-requests` creates a `DISCOVERED` contribution with `source='hermes_request'` (for operators too), subject to the same re-discovery rules. There is no separate requests table.
- `POST /v1/actions` is refused (423) while paused or killed. The body is normalized and gets `<!-- ossbot:action=<id> -->` appended at storage time. A secret or Kiro-key hit is reported as anomaly `secret_in_text`: the action is blocked, and a second hit within 24 h pauses.
- The targets denylist no longer carries the work account (no personal values in tracked files). `policy.check_repo` refuses any repo owned by an effective forbidden login (profile union floor) instead.
