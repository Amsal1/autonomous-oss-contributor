## Summary

<!-- What changes and why. Link the issue. -->

## Tests run

<!-- Commands and results, e.g. lint gate, `pytest -m "unit or telegram or acp or e2e" -n auto`, targeted tests, live checks. -->

## AI assistance

- [ ] No AI tools were used.
- [ ] AI tools were used. Tools and what they did: <!-- e.g. "Kiro: drafted tests/unit/x.py; I reviewed and edited it" -->
- [ ] I have read and understood every line of this change and take responsibility for it.

## Invariants checklist

- [ ] No invariant I1-I13 (AGENTS.md) is weakened; a change to one comes with a new ADR.
- [ ] No new GitHub write path: writes still happen only in `gatekeeper/submit.py` → `gatekeeper/github_writer.py`.
- [ ] Approvals stay two human taps, bound to content and mode, with no LLM in the path and no API endpoint.
- [ ] No exposure: no inbound port, webhook, tunnel or Mini App.
- [ ] LLM and upstream output stays data (pydantic, escaped, never executed); argv lists only.
- [ ] No secret or personal value in a tracked file (`ossctl lint personal`, gitleaks).
- [ ] Docs, ADR and `config/versions.lock.yaml` updated where needed.

## Sign-off

- [ ] Every commit is signed off (`git commit -s`, Developer Certificate of Origin).
