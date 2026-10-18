# CR-003: findings carried into the build (orchestrator decision, 2026-10-18)

I ended the CR-003 design loop after round 6 instead of running another design pass. The design decisions are settled; the reviewer confirmed all HIGHs resolved, ADR-0020 in place and the Pillow decision made. The remaining items are rendering and implementation details, which the build handles better as tests than as more prose.

Each item below is a **required build acceptance criterion**. The consolidation step must add every item to docs/TRACEABILITY.md as its own row, with a test:

- Every finding in `docs/ux/cr003-verdict.json` (round 6: 1 MEDIUM, 5 NIT), as committed at the point this file was written.
- **Round-6 MEDIUM, the binding rule:** the PR title is publish bytes, so a card may never cut or drop it. Wrap it to as many lines as it needs; if a budget is short, shorten other card sections instead. Mockups 01 and 03 must be updated to show the full title. Add a test with a 256-char title that asserts the card shows the exact title bytes and the approval hash matches them.
- Any "Build acceptance criteria carried from review" section already in CR-003.

Rule for later design or review loops in this repo: block only on design-level HIGH/MEDIUM correctness or safety issues. Turn detail-level findings into acceptance criteria instead of more design rounds.
