# FEAT-010 build review, round 1

Verdict: CHANGES_REQUESTED

Scope: commits after the FEAT-009 approval (883affb): 680c97a (item 49 streaming assistant) and addfd90 (item 49 U12, feature cards, Hermes notices). HEAD = origin/main = addfd90, so nothing local is missing from the remote and history is linear. There was no prior review file. No suites were re-run.

## Evidence checked

- `git status --porcelain` at review time: 16 modified tracked files and 11 untracked files. These include `migrations/0006_telegram_m2.sql`, `telegram/{digest,digest_cmd,reminders,status_card}.py`, `api/reminders.py`, and tests `test_digest.py`, `test_reminders.py`, `test_rich_fallback.py`, `test_status_card.py`, `test_outcomes_digest.py`. Together they make up uncommitted item 50 work, with no recorded test evidence.
- `docs/plan/phase2-build-plan.md` lines 270-296: items 49-53 are all still `[ ]`. `FEAT-010.json` has `"status": "pending"`.
- `docs/VERIFICATION.md` has no FEAT-010 entry. `docs/TRACEABILITY.md` rows for FEAT-010 (for example DES-FR-13 and DES-AC-36) still read `missing`.
- Item 49 code was spot-read, with no defect found:
  - `handlers.on_raw` (handlers.py:903-918) gates Stop with `authorized_chat_only` and stops only a `streaming` turn that has a live task and a stop event.
  - `ALLOWED_UPDATES` includes `stopped_message_generation` (bot.py:54).
  - `hermes_out.resolve`/`deliver` refuse without an active turn, keep Reviews, Control and Feed sessions out of those topics, and cap Hermes P1 at 5 per local day.
  - `api/notify.py` rejects any priority other than P1/P2 and returns 409 without an active turn.
  - `api/features.py` returns 404 for a feature that isn't enabled, validates with the feature's model, returns 422 without echoing the data, enforces the header rule and the 30/h limit.
- Commit messages record targeted verification: 593 telegram/chat passed plus 197 U12 tests, ruff, mypy and lint-imports clean. That is acceptable per-item evidence under rule 9. The once-per-feature hermetic suite and the FEAT-010 verification list are not recorded.

## Blockers

1. F1 (HIGH), uncommitted WIP in the working tree. Working rule 4 forbids approval while the tree is dirty. Rule 1 says the next coder step must finish, test and commit this item 50 work (or revert it with a note in VERIFICATION.md). Do not discard it.
2. F2 (HIGH), FEAT-010 is incomplete. Items 50 (rich views, status card, reminders, digest, migration 0006), 51 (review mirror, off by default), 52 (Telegraph, off by default) and 53 (close-out) are not committed. Several FEAT-010 acceptance criteria have no implementation yet: AC 24, 27, 28, 42, 64, 65 and 68, plus mirror and Telegraph off by default. None of the following is recorded: the FEAT-010 verification list (`DEV pytest -m "unit or telegram or acp or e2e"`, the lint gate, provision twice with the second run `unchanged`, the egress surface test, doctor), the owner-only live checks, TRACEABILITY and VERIFICATION updates, or FEAT-010 `completed`. Implement items 50-53 and record that evidence.

## NITs

None.
