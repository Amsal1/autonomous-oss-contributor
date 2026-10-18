# Consolidation fix pass: DESIGN revision 5, traceability matrix and Phase 2 plan (pass 2)

This is the second review of the consolidation. Commit `e0ecf11` answers the eleven pass-1 findings (C-1…C-11, `b0402b4`). It rewrites the stale FEAT ids in §14 and ADR-0019 and splits the §14 data model over migrations 0002–0006, each with one owning PLAN item. It amends the threat model (§8.4 T1/T2 and T6, §8.5) and the §17.1 targets schema, adds the `CR3-IH-1…18` rows for the §14.19 integrations, aligns PLAN item row lists with the matrix phase column, and moves the revision-4 Telegram layouts into a historical Appendix A. All five blocking findings are resolved (confirmed against HEAD). DESIGN is one source of truth, the matrix has 508 fully populated rows, and PLAN keeps the safety-rework → backend → Telegram → docs order.

Watch for: four bookkeeping NITs in TRACEABILITY.md. The totals line is off by one (confirmed). Row CR3-BA-13 still says "FEAT-005's `edit_comment`" (confirmed). Row `CR3-v1.x` duplicates `CR3-IH-18` and no PLAN item names it (confirmed). Two rows use a phase value outside the legend or are `done` while their tests point at open rows (confirmed). None of these blocks the Phase 2 build.

**Verdict**: APPROVED

## High-level view

On (a), §14 is now replaced by CR-003. §14.1–§14.3 are the current rules with the CR-003 amendments folded in. §14.4, §14.4a and §14.5 keep only the FR-5 card content, the per-kind content and the registry mechanism, and the revision-4 layouts, keyboards and command table sit in a non-normative Appendix A. The section numbers §14.1–§14.5 survive only because Phase 1 docstrings cite them, and PLAN item 44 (TGRW-15) repoints those docstrings. Every FEAT reference in §14 and ADR-0019 names a current PLAN item. The threat model and §17.1 now match the revision-5 defaults (open-mode `web_public` for `agent-llm`/`catalog`, the per-tier Hermes surface behind the skill gate, typed-block rendering, no per-target line cap). Every CR file and NOTE-live-secrets carries a "Merged into DESIGN.md §…" header.

On (b), the known rework items keep their correct status. The ceiling (CR4-2g, CHAT-31), the per-target cap (CR4-2o), github.io and gists (CR2-1a, CHAT-24) and default `open` (CR2-6b, CHAT-23) are `rework` in FEAT-005, each with the non-conforming config line as evidence. The self_improve allowlist (CR2-3d, CR2-6a, ADR-0018) is `missing` in FEAT-007, because Phase 1 built no Hermes. For the library, ADR-0020 is `done` with no swap and ADR-0020b (`rawapi.py`) is `missing`. TGRW-1…15 audit the FEAT-002 code against §14, CR1-6a/6b cover lifecycle 6a/6b, and P1-15…P1-19 cover the round-6 NITs. A mechanical check of each row's phase column against the PLAN item row lists found one orphan row (`CR3-v1.x`). The other three issues are counting and vocabulary drift.

On (c), PLAN runs FEAT-005 (ceiling, egress modes, token core, handler budget and redactor, NITs, no-exposure guard), then FEAT-006/007/008, FEAT-009/010 and FEAT-011. TGRW-9 now sits in FEAT-010 item 49 with the Stop handler that needs it, so the FEAT-005 close-out gate can pass. The §14.19 v1.x scope is stated the same way in §14.20.8, §6.1 and PLAN "Deferred": Telegraph, PDF reading and the voice fallback ship, and the rest is deferred.

On (d), `design-review.md`, `cr003-review.md` and the VERIFICATION CR tables are marked historical. A scan of the FEAT JSONs found no carried planner-draft text that still conflicts with the item text (`0002_phase2`, a `locked` default, Pillow in the gatekeeper, old ceilings).

<details>
<summary>Issues (4)</summary>

1. **Totals line off by one** (NIT): TRACEABILITY.md line 14 says 507 rows and 303 `missing`, but the table has 508 rows and 304 `missing` (done 110, rework 68 and deferred 26 match). Fix the totals.
2. **Stale FEAT id in row CR3-BA-13** (NIT): the requirement text still reads "Replace calls FEAT-005's `edit_comment`…", while DESIGN §14.21.1 BA-13 now says "the S7 `edit_comment` service function (PLAN item 45)". Re-copy the row text from DESIGN.
3. **Orphan row `CR3-v1.x`** (NIT): it duplicates `CR3-IH-18` (Telegraph, FEAT-010) and no PLAN item names it, so the VERIFICATION claim that every open row is named by a PLAN item is slightly off. Merge it into CR3-IH-18, or add it to item 52's row list.
4. **Matrix vocabulary drift** (NIT): CHAT-34 has phase `done`, which the legend does not allow (use `decision` or `P1`). DES-TM-1 is `done` while its phase names FEAT-005 and FEAT-007 and its tests cell points at open rows (P1-18, CR2-6c, CR2-3d). Either make it a `decision`-phase docs row, or keep it open until those rows close, the same rule pass 1 applied to CR3-S4.

</details>

<details>
<summary>Details</summary>

### Remaining bookkeeping in TRACEABILITY.md

The matrix is the stated status source for every close-out gate, and FEAT-011 item 57 checks it mechanically, so its counts and vocabulary have to be exact. Parsing the table shows four small drifts (all confirmed):

```
line 14   Totals: … missing 303, … **507 rows**     table: 508 rows, missing 304
CR3-BA-13 "Replace calls FEAT-005's `edit_comment`…" DESIGN §14.21.1: "(PLAN item 45)"
CR3-v1.x  P2 FEAT-010, missing, not in any item     same scope as CR3-IH-18 (item 52)
CHAT-34   phase `done`                              legend: P1 | P1-rework FEAT-005 | P2 FEAT-0nn | deferred | decision
DES-TM-1  status `done`, phase FEAT-005 + FEAT-007  tests: P1-18, CR2-6c, CR2-3d (all open)
```

CR3-BA-13 is the last trace of C-1. DESIGN, ADR-0019 and PLAN are clean, but the matrix row is a truncated copy of the pre-fix text. A builder reading only the matrix would look for an `edit_comment` service in FEAT-005, where none is planned.

DES-TM-1 is a docs-text row and its text is amended, so marking it `done` can be argued. It still conflicts with two of the matrix's own rules: `done` means proven by the named tests, and on a multi-owner row "the last owner sets `done`". FEAT-005 item 27 updates the matrix anyway, so all four fixes can land in its first commit.

</details>

<details>
<summary>File map</summary>

- `docs/DESIGN.md`: §8.4 T1/T2 and T6, §8.5 amended; §6.1 deferred §14.19 row; §14 preamble rewritten; §14.1–§14.5 trimmed to the current rules, revision-4 text moved to Appendix A.1–A.3; FEAT ids in §14 rewritten to PLAN items; §14.20.2 migration split 0002–0006; §14.20.8 FEAT map and v1.x scope; §17.1 `size_class`; §27.0b responses to C-1…C-11.
- `docs/TRACEABILITY.md`: rows `CR3-IH-1…18`, `DES-TM-1` and `TGRW-15` added; phases aligned with PLAN; evidence on every `rework` row; CR3-S4 set to `missing`; P1-15/16/18 test cells.
- `docs/PLAN.md`: full row ids on every item; TGRW-9 moved to item 49; migrations per item; §14.19 scope in items 37, 49, 50 and 52 and in "Deferred".
- `docs/adr/0002`, `docs/adr/0019`: title and FEAT-reference fixes.
- `docs/plan/task-ossbot-v1/features/FEAT-005…011.json`: regenerated item text; conflicting carried draft parts rewritten.
- `docs/VERIFICATION.md`: consolidation fix-pass note.

Full diff: `git show e0ecf11`.

</details>
