# Post-v1 final review (range 8c26750..ac91e58, branch main)

Verdict: **APPROVED** (no HIGH or MEDIUM finding). Reviewed from the diffs, the plan, the three package
reviews and verify-a/backend/oss/final. No suite, lint, gitleaks, redeploy or live check was re-run; one
narrow spot-check (LICENSE bytes against the recorded hashes) was done.

## Evidence

- Close-out (verify-final.md at ac91e58): lint gate green (ruff, format 515 files, mypy 245 files,
  lint-imports 12 kept), shellcheck 41 files rc 0, `ossctl lint personal` 0 hits over 836 tracked files
  (strict tier for employer values, `docs/` included), `ossctl docs commands --check` current, gitleaks
  history 228 commits no leaks (`gitleaks dir` aborted by the owner over drvfs; the history scan covers
  every tracked blob). One hermetic run `-n auto`: 3647 passed. One redeploy: exit 0, doctor PASS 40 /
  WARN 1 (transient Hermes restart), later PASS 41.
- Item 2 live: `ossctl hermes probe --cancel-check` → `stop_reason: cancelled`, 0.01 s, pass; owner Stop
  in the real chat, no "in-flight request may keep running" line. O5 / CR3-V-7 set to done.
- Item 3 live: `/status` is the same icon card as the pinned dashboard in Control and All; the pin lands
  in Control (`dashboard_pinned thread=125003`) and is edited in place. Open: in All a stray empty
  "/status" topic is created (LOW-1).
- Simulated WM_QUERYENDSESSION: 50 PASS / 0 FAIL; the drain goes through the channel
  (`down kind=graceful caller=blocker … (channel)`, exit 0, 10.1 s); `status.ps1` reports
  `last shutdown graceful (marker 2026-10-20T17:41:12Z …)`, i.e. the real marker, not "unknown".
  Lifecycle 62/0, ossctl logs 21/0.
- Docs: TRACEABILITY (PV1-A1…A8, PV1-B-*, CR3-V-7 done, CR3-V-4 reopened, totals recomputed),
  VERIFICATION post-v1 section, FINAL_REPORT (O1 done with the pin outcome plus the open `/status`-in-All
  limitation, O3 done, O5 done, R1 steps and `record-owner-check.ps1 -Check R1`, not marked passed),
  README (Threaded mode, `ossctl.ps1 logs`, Contributing, License).

## Range re-check

- Part A 1-8: drain channel opened at logon with 6b retired (ADR-0021 addendum), ACP `session/cancel`
  with 10 s bound (ADR-0002 addendum), one dashboard builder for `/status` with own tokens, pin only in
  Control, Threaded mode wording, `ossctl.ps1 logs`, Hermes stop/context/title/stamp fixes. review-a
  APPROVED; its LOWs are carried below.
- ModelBackend: `common/backends.py` is pure data; `runner/llm.py` re-exports the moved code;
  `pipeline.kiro()` selects the backend with the old fallback; `scan_secrets` yields exactly
  `KIRO_API_KEY` + `GITHUB_READ_TOKEN` as before; `backend: kiro` is the default. New import-linter
  contract keeps `runner.backends` away from gatekeeper/sandboxd/dockerfilter/ctl. No change to the
  gatekeeper write path, approvals, state machine, probes list, Docker filter or key-scan needles.
- Open source: LICENSE starts with the apache.org bytes (sha256 `cfc7749b…3d30` prefix; whole file
  `ed81f018…03d3`, re-hashed here and equal to the record), NOTICE credits Amsal Khan with the repo URL,
  THIRD_PARTY_NOTICES, CONTRIBUTING (incl. "AI-assisted contributions" and "Adding a model backend"),
  Contributor Covenant 2.1, SECURITY, issue/PR templates, pyproject PEP 639 fields.
- Personal values: no owner-specific value in any tracked file; the strict `lint personal` tier keeps
  it that way (values come only from the gitignored `config/profile.yaml`).
- Invariants I1-I13: no diff in `submit.py`, `github_writer.py`, approvals, `state_machine.py`, policy
  ceiling or control; `policy.yaml` only gains `hermes.context_length`. Dashboard copy taps are refresh,
  pause and two-tap kill (stop-class only). Dry-run stayed ON per every verify file.
- Git: 18 commits, all `Amsal Khan <md.amsalkhan@gmail.com>`, no rewrite, origin URL unchanged, no repo or
  visibility change, tree clean. `origin/main` moved to `ad8835c` by pushes after each package gate. That
  fits the owner pushing himself (same pattern before the baseline); `ac91e58` is not pushed.

## Findings (none blocking)

| Sev | File | Issue | Fix |
|---|---|---|---|
| LOW | src/ossbot/gatekeeper/telegram/commands/builtin.py | With Threaded mode on, `/status` sent in All creates an empty "/status" topic and the card lands in All (PV1-A3 / CR3-V-4 `missing`). Documented, deferred by the owner (followups.md). | Inspect the update's `message_thread_id`/`is_topic_message` in All and route the reply (or Control); add a case to `tests/telegram/test_status_is_dashboard.py`; owner re-checks O1. |
| LOW | LICENSE | The verbatim Apache text is followed by a 3-line pointer to NOTICE. GitHub's license detector needs about 98 % similarity, so the extra lines may stop it from showing "Apache-2.0". | Move the pointer into README/NOTICE so LICENSE is the exact apache.org file, or check the repo sidebar after publishing. |
| LOW | scripts/windows/ShutdownBlocker.cs | (carried from review-a) A channel that dies at once is restarted every 1-2 s without back-off. | No restart sooner than 60 s after a channel that lived under 10 s; log the repeat once. |
| NIT | src/ossbot/ctl/lint_personal.py | (carried from review-oss) Bare-domain floor entries are not in the strict tier. | Treat domain-shaped entries as strict too. |
