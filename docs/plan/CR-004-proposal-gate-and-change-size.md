# CR-004: Proposal gate before implementation, plus a change-size budget

**Merged into DESIGN.md §9.1, §9.2, §9.5a, §9.5b, §9.14 and §12.6 (with FR-3, FR-20, FR-21 and AC 37–41; presentation in §14.11.15–§14.11.19 and §14.14.4) on 2026-10-18, Amendment B included (it overrides the "Original §2" text kept below). Historical; do not edit.** The current spec is DESIGN.md; requirement rows are `CR4-*` in `docs/TRACEABILITY.md`.

Status: Required for Phase 2 (user request, 2026-10-18). Where it conflicts with DESIGN.md §9 (pipeline), §12 (state machine) or config/policy.yaml, this CR wins, and DESIGN.md and its ADRs must be updated to match. The Telegram presentation of the proposal card belongs in CR-003.

## Problem
The pipeline today goes discover → vet → plan → implement → validate → review → **preview card** → approve → submit. The only human gate is at the very end, after all the work is done. The user wants two things:
1. A cheap, early gate. The bot researches the repo and the candidate first, then asks for approval to *start* with a high-level plan. No implementation compute is spent and nothing is cloned for building until the user says yes.
2. A tunable size budget. Small, focused changes are preferred, because they are more likely to be merged and build a track record with maintainers. Today `policy.diff.max_files: 25` / `max_changed_lines: 600` are hard safety caps, not a preference.

## Requirements

### 1. New pipeline shape
`discover → vet → scout → PROPOSAL (human gate 1) → plan → implement → validate → self-review → PREVIEW (human gate 2, unchanged) → submit → watch`

- **scout** (new, LLM, `research` profile, read-only, no code execution) builds a codebase overview of the target repo, using a shallow, read-only clone or snapshot:
  - layout and the modules relevant to the candidate
  - build/test tooling and how CI runs
  - code conventions
  - recent merged PRs: typical size, style, review latency, who merges
  - CONTRIBUTING and AI-policy findings from vet
  - open PRs touching the same area (duplicate/conflict check)
  - how the change would be validated locally (T0 unit tests / T1 project run / T2 kind/compose) and how hard that is

  Scout produces a **proposal**:
  - the problem, in one paragraph
  - the proposed approach, 3–6 bullets
  - files/areas likely touched
  - **estimated size**: files, changed lines, plus a size class
  - tests to add or update
  - the validation plan
  - risks and open questions
  - why the bot thinks it will be accepted (maintainer signals)
  - confidence score
  - alternatives considered

  Scout runs with web access (CR-002 `open` mode) to read upstream docs, the issue thread and linked discussions.
- **PROPOSAL** gate (deterministic gatekeeper, same two-tap single-use-token mechanics as PR approval).
  - Buttons:
    - ✅ Start work
    - ✏️ Adjust plan: free-text feedback → scout revises → a new proposal version, with a diff against the previous one
    - 📏 Make it smaller: asks scout for a reduced scope that fits a smaller size class
    - ⏭ Skip
    - 🚫 Never this repo / never this kind
    - ⏰ Later: snooze N days
  - An approved proposal is bound by hash to the next stages. Scope drift is **flagged, not blocked** (see §2):
    - files outside the proposed areas are listed on the preview card
    - a measured size above the estimate by more than `size.overrun_tolerance` (default 50%) is highlighted on the preview card with the reason the coder gives
    - only the hard safety ceiling stops the pipeline
  - Proposals expire after `proposal.ttl_hours` (default 72) and are then re-scouted if still relevant.
- **Issue claim comment** on upstream (optional, `proposal.claim_comment: off` by default). When enabled, it is only posted after gate 1, and it is itself a public write. It goes through the same approve+confirm. Some repos expect "can I take this?" first. Vet records whether the repo's CONTRIBUTING requires assignment before work. If it does, the proposal says so and offers "Ask maintainers first" as an action.
- Dry-run applies to the claim comment like any other write.

### 2. Size: a soft preference for autonomous discovery, never a rule against the user

**Amendment B (user decision, 2026-10-18). It overrides the original §2 text below wherever they conflict.**
- Size classes are **estimates and ranking preferences**. They shape what the bot picks *on its own*. They are not limits on what it can do.
- **User-directed work bypasses size preferences and filters entirely.**
  - **How the user assigns work:**
    - `/work <issue/PR/repo URL> [notes]`
    - forwarding a GitHub link to the bot and tapping "Work on this"
    - `ossctl work <url>`
  - **What a user-directed item goes through:** scout → proposal (gate 1) → … as usual, marked `👤 requested by you`. Large changes (`l`, `xl`, or bigger) are fine.
  - **What a user-directed item skips:** ranking, the daily caps and the size class filter.
  - **What still applies (safety and etiquette):**
    - **forbidden paths**
    - **an AI-contribution ban:** the bot explains it and refuses, because the PR would break the project's rules
    - **a CLA:** shown, and the user must confirm it is signed
    - **one open PR per repo:** shown, and the user may override it with a confirm
- **Classes (estimates)**, counting *non-test* changed lines, excluding generated files, lockfiles, snapshots and vendored code:

  | Class | Changed lines | Files |
  |---|---|---|
  | `xs` | ≤ 20 | ≤ 2 |
  | `s` | ≤ 80 | ≤ 4 |
  | `m` | ≤ 250 | ≤ 8 |
  | `l` | ≤ 500 | ≤ 15 |
  | `xl` | ≤ 1000 | ≤ 30 |
  | `xxl` | anything above `xl` | anything above `xl` |

  Test lines are tracked separately and **never count toward the class**.
- **Autonomous discovery:** `size.autonomous_max_class` (default `s`) and `size.preferred_classes` (default `[xs, s]`) affect ranking and which candidates get proposed. Scout may also offer a smaller-scope alternative next to a bigger one.
- **Overrun:** `size.on_overrun: flag` (default) highlights estimate-vs-actual on the preview card. `reapprove` (opt-in) sends the item back to gate 1 when the actual class exceeds the approved one. `RESCOPE_REQUIRED` exists only for the `reapprove` mode and for the hard ceiling.
- **Hard safety ceiling** (`policy.diff`): a runaway-agent guard, not a size preference.
  - **Raise it** to `max_files: 80` and `max_changed_lines: 3000` (non-test) plus `max_test_lines: 3000`.
  - **Change it:** config file only (not Telegram), so a prompt injection or a mis-tap can't raise it. It also applies to user-directed work.
  - **Hitting it:** the item pauses with a card explaining why and suggesting the user split the work or raise the ceiling in config.
- **Tests are expected, not optional:**
  - When the repo has a test suite, scout's proposal must list the tests to add or update. The coder must add or update tests covering the change: regression tests for bug fixes, unit and integration tests for features, following the repo's existing test style and location.
  - Validation runs them and the self-review checks that they exist and actually exercise the change. Missing tests are a self-review rejection, unless the change is docs-only or config-only or the repo has no tests (the reason is stated on the card).
  - The preview card shows code lines and test lines separately, e.g. `+142 / −30 code · +210 test`.
- **The rest of the original §2 is superseded where it conflicts:** the class table, the `max_class` default and the Telegram-tunable `max_class`.
  - **What becomes `autonomous_max_class`:** `/size` sets this. Per-repo overrides apply to autonomous discovery only.
  - **What stays the same:** the filters (`allowed_kinds`, `excluded_kinds`, `require_labels_any`, and so on) apply only to autonomous discovery.

#### Original §2 (kept for reference; read with Amendment B above)
- New `policy.size` block:
  - `classes` (defaults, each defined by max files / max changed lines excluding tests, generated files and lockfiles; tests counted separately):
    - `xs`: docs/typo/config, ≤2 files, ≤20 lines
    - `s`: small bug fix, ≤4 files, ≤80 lines
    - `m`: focused feature/fix, ≤8 files, ≤250 lines
    - `l`: ≤15 files, ≤500 lines
  - `max_class` (default **`s`**), above which candidates are not proposed
  - `preferred_classes`, used in ranking (default `[xs, s]`)
  - `max_test_lines` (separate allowance, default 2× the class line budget)
  - `overrun_tolerance` (default 0.5)
  - `exclude_globs` (generated files, lockfiles, snapshots, vendored code)
- The existing `policy.diff.max_files/max_changed_lines` stay as the **hard safety ceiling** and can't be raised from Telegram. `size.max_class` must be ≤ the ceiling; config validation enforces this.
- **Where size is enforced:**
  - (a) discovery ranking: estimate from issue text, labels and linked code; drop candidates clearly above `max_class`
  - (b) scout's estimate: refuse to propose above `max_class`; may propose a reduced scope instead
  - (c) after implement: measured diff vs the approved class and estimate (deterministic, gatekeeper-computed numstat)
  - (d) the preview card shows estimate vs actual
- **Other filters, all in config:**
  - `allowed_kinds` (default `[bug, docs, tests, small-feature, ci-fix, refactor-small]`)
  - `excluded_kinds` (default `[breaking-change, new-dependency, api-redesign, security-sensitive-crypto]`)
  - `require_labels_any` (e.g. good first issue, help wanted, bug; configurable per target)
  - `max_issue_age_days`
  - `min_maintainer_activity`
  - `max_open_prs_by_me_per_repo` (existing: 1)
  - `languages`
  - `validation_tier_max`: T0 only, T0–T1, or T0–T2; default T1
- **Tuning UX** (Telegram, deterministic; presentation in CR-003):
  - `/size`: shows the current class and lets you pick `xs`/`s`/`m`/`l` with inline buttons
  - `/filters`: the toggle list for kinds/labels/validation tier
  - per-repo overrides via `/repos <repo> size m`
  - `ossctl size set` / `ossctl filters`

  Every change is audited and FYI'd. Changing size or filters never affects already-approved proposals, which keep their approved class.

### 3. Ranking and pacing (favouring small, proven, likely-to-merge work)
- **Rank score:**
  - size fit (prefers the smallest class that solves the issue)
  - maintainer responsiveness (median time-to-merge for outside PRs)
  - outside-contributor merge rate
  - labels inviting outside help
  - issue freshness and clarity (reproduction steps present)
  - the user's interest categories (profile.yaml)
  - the user's prior history in that repo (prefer repos where earlier PRs merged; cool down after a close-unmerged)
- **Discovery output:** a ranked shortlist; send the top `discovery.proposals_per_cycle` (default 3) as proposal cards. A daily cap on how many proposals are sent (`proposal.max_per_day`, default 5) avoids flooding.
- **Track record:** record proposal → PR → merge outcomes per repo, kind and size class. The digest (CR-003) shows the merge rate by size class, so the user can tune `max_class` with data.

### 4. Pipeline batching
While a proposal awaits the user, nothing else runs for that candidate. Scouting several candidates in parallel is fine (bounded by `max_concurrent_scouts`, default 2). Implementation runs only for approved proposals, bounded by the existing concurrency limit.

### 5. State machine and tests
- **New states:**
  - `SCOUTING`
  - `PROPOSED` (awaiting gate 1)
  - `PROPOSAL_APPROVED`
  - `PROPOSAL_REJECTED`, `PROPOSAL_SNOOZED`, `PROPOSAL_EXPIRED`
  - `RESCOPE_REQUIRED` (when the measured size exceeds the approved class)
- Re-discovery rules extend M6. Never re-propose after "never"; after "skip", only on a material change to the issue; after "snooze", after the snooze period.
- **Unit tests:**
  - every transition
  - the proposal approval token bound to the proposal hash
  - an edit invalidates it
  - a size-class overrun → `RESCOPE_REQUIRED`
  - `max_class` above the ceiling is rejected
  - Telegram size/filter changes are audited and leave approved proposals untouched
- **e2e dry run** against the fake upstream: discover → proposal card → Start work → … → preview → approve (DRY RUN).
- Kill/pause/drain stop scouting too. Lifecycle `down` re-queues an in-flight scout.

### 6. Docs
README "How the bot picks work" section; AGENTS.md invariants (two human gates, size ceiling not Telegram-raisable).
