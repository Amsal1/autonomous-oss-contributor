# ADR-0015: Open-source readiness: personal profile file, monotonic personal floors, README/AGENTS, CI
Status: Accepted · Date: 2026-10-17 · Design: DESIGN.md §7.9, §18.5, §18.8, §18.9

## Context
The repo now has a private remote and may be published later. v1 put personal values in tracked config (`identity.yaml`, IdC start URL in `egress.yaml`, `@Amsal1` in templates) and hard-coded `<work-login>` / `@<work-domain>` floors in `policy.py`.

## Decision
- All personal values (GitHub login, commit name/email, forbidden logins, confidentiality strings, Telegram user ID, IdC start URL/region, backup recipients, interests) live in the gitignored `config/profile.yaml`; `config/profile.example.yaml` with placeholders is committed. `profile.yaml` is loaded through the human-confirmed config snapshot like policy and targets. Templates use `{{ profile.* }}`; tests use a fixture profile.
- Personal floors are monotonic instead of hard-coded: every forbidden login and confidentiality string ever loaded is kept in the gatekeeper's `policy_floor` table; removal needs an operator TTY command plus a Telegram confirm. An empty `forbidden_logins` on a fresh DB fails closed. The non-personal workflow-path floor stays in code.
- `ossctl lint personal` (pre-commit, after gitleaks) fails if any local profile value appears in tracked files outside `docs/`; CI checks the example holds placeholders.
- README.md (safety model, mermaid diagram, quickstart, generated command table, limitations, AI-disclosure statement, License: TBD) and AGENTS.md (architecture map, conventions, invariants, how-to recipes, testing) are specified deliverables.
- `.github/workflows/ci.yml`: lint, hermetic tests, gitleaks (pinned binary, full history); `permissions: contents: read`; actions pinned by SHA; no secrets.

## Alternatives
- Keep identity in tracked config: rejected (publishing would leak personal data and the work account name).
- Keep hard-coded floors: rejected (personal values in code); monotonic DB floor preserves the "Windows edit cannot silently weaken" property from review M5.

## Consequences
A fresh clone does not work until the profile is created or restored (SETUP step 4/7). `docs/` may still name the owner's deployment; that is intentional and excluded from the lint.
