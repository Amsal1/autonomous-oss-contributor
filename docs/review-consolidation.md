# Consolidation of CR-001…CR-004 into DESIGN revision 5, traceability matrix and Phase 2 replan

Commit `b0402b4` merges CR-001, CR-002 (+A), CR-003, CR-004 (+B), NOTE-live-secrets and the chat decisions into DESIGN.md revision 5, amends seven ADRs, adds `docs/TRACEABILITY.md` (488 rows, with a Phase 1 code audit), and rewrites PLAN.md Phase 2 as FEAT-005 (Phase 1 rework) followed by FEAT-006…011. CR-003 lands as §14.6–§14.25 with a numbering map, every CR file carries a "Merged into DESIGN.md §…" header, and the matrix covers every FR, NFR, AC, invariant, §23 verify item, CR-003 D/U/AC/BA/V item and round-6 finding I checked. Four gaps keep it from being a single source of truth the Phase 2 builder can follow without guessing.

Watch for: the CR-003 text merged into §14 still uses the old PLAN numbering, so "FEAT-005" and "FEAT-009" inside DESIGN now point at the wrong FEATs (confirmed); the threat model (§8.4, §8.5) and the targets schema (§17.1) were not amended and contradict the default egress mode, the default Hermes tier and the removed per-target line cap (confirmed); the §14.19 integration-hook table has no traceability rows, so its v1.x items are neither planned nor deferred (confirmed); and several PLAN items close rows that the matrix assigns to a different FEAT, including one that blocks the FEAT-005 close-out gate (confirmed).

**Verdict**: NEEDS_CHANGES

## High-level view

DESIGN revision 5 is structurally right: one document, CR-003 as §14.6–§14.25 with a CR→DESIGN section map, §14.1–§14.5 kept as the amended revision-4 baseline with a "later section wins" rule, and ADR-0002/0003/0004/0005/0008/0009/0018 carrying revision-5 amendment blocks. The merge was done section by section, though, and two kinds of residue remain. The verbatim CR-003 text references FEATs by their pre-consolidation numbers (old FEAT-005 was the Telegram decision-path FEAT, old FEAT-009 was CR-002), and sections outside the CR targets, mainly the threat model and the targets schema, still describe revision-4 behaviour that revision 5 changed.

The traceability matrix is the strongest part of the change. All 488 rows have the eight columns, the totals line matches, and the known rework items are present and correctly classified: the ceiling (CR4-2g, CHAT-31: 25/600 → 80/3000/3000), the hidden per-target cap (CR4-2o), github.io and gists (CR2-1a, CHAT-24), default `open` (CR2-6b, CHAT-23), the self_improve allowlist (CR2-3d, CR2-6a, ADR-0018), the library decision (ADR-0020 done with no swap, ADR-0020b adapter missing, CHAT-22), the §14 audit of the FEAT-002 code (TGRW-1…14), lifecycle 6a/6b (CR1-6a/6b, FEAT-008) and the five round-6 NITs (P1-15…P1-19). Spot-checks of the audit claims against the code (approvals.py, policy.py, common/config.py, egress.yaml, telegram/bot.py, policy.yaml) all held. The gap is the §14.19 integration-hook table, which has no rows, and a set of weaker cells: rework rows with no evidence pointer, and generic locations like "see §14.20.1".

PLAN.md follows the required order (FEAT-005 safety rework → FEAT-006/007/008 backend → FEAT-009/010 Telegram → FEAT-011 docs). For a handful of rows, though, the item and the matrix disagree about which FEAT owns them, and that matters because each close-out gate is defined against the matrix.

CR files, `design-review.md` and `cr003-review.md` are marked historical, and `cr003-carried-findings.md` points at its matrix rows. The FEAT JSON files still carry labelled "carried detail from the interrupted planner draft" blocks, and one of them is the only place a needed table is specified.

<details>
<summary>Issues (11)</summary>

1. **Stale FEAT ids in merged §14** (blocking): "FEAT-005 item 22/23", "FEAT-005's edit_comment", "FEAT-009 tier switch", "FEAT-009's skill_reviews" and the `pending_input` / `0002_phase2.sql` note in §14.9, §14.10.4, §14.11.1, §14.15, §14.18, §14.20 and §14.21.1 (and ADR-0019 line 220) use pre-consolidation numbering. Rewrite each to the current PLAN item (24, 36, 45), and give `pending_input`/`chat_sessions` an owning item and a non-colliding migration number.
2. **Threat model not amended for rev 5** (blocking): §8.4 T1/T2 says `agent-llm` "reaches only Kiro hosts" (false under default `open`), T6 says `skills`/`web` are disabled (default tier `self_improve` enables `skills`), and §8.5 says Hermes text is "escaped text only, never buttons" (D8/D9). Amend all three and track them in a matrix row.
3. **§17.1 still has the per-target line cap** (blocking): `defaults.max_changed_lines: 400` contradicts §12.6 and CR4-2o. Replace it with the per-repo `size_class` preference.
4. **§14.19 integration hooks untraced** (blocking): none of the 18 items has a row. The v1.x items #6–#10 are neither planned nor deferred, the #1 digest stats are only partly traced, and #12–#14 and #17 are missing from `DES-DEF-*`. Add one row per item and state in §14.20.8/FEAT-010 which v1.x items Phase 2 builds.
5. **PLAN/matrix FEAT ownership mismatches** (blocking): TGRW-9 (matrix FEAT-005, built in FEAT-010 item 49, so the FEAT-005 close-out cannot pass), TGRW-11, CR3-U13, CR3-D10, CR3-V-13, CR3-U9, and the unnamed CR3-D5/D6/U4/DES-FR-14. Align the phase column and the item row lists.
6. **§14.1–§14.5 kept beside §14.6+**: two specs with a precedence rule rather than a replacement. Consider moving the baseline into a "superseded" appendix and updating the citing docstrings in FEAT-009.
7. **Rework rows without evidence**: 21 `rework` rows and `done` CR3-CF-2 have evidence `–`. Point them at the matching TGRW row or code line.
8. **CR3-S4 `done` without proof**: its test (DES-AC-42/CHAT-33/CR3-CF-1) is still `missing`. Set it to `missing` or link it to those rows, and fix the copied "approvals.py, handlers.py" location on S1–S4.
9. **Non-test cells in the tests column**: P1-15, P1-16 and P1-18 name config or script files or "review". Name the golden, doctor check or VERIFICATION entry that proves each one.
10. **Draft text in FEAT JSONs**: prune the conflicting carried parts (FEAT-009 draft items 2 and 3: `0002_phase2.sql` and the revision-4 `/approve` N4 semantics).
11. **Minor DESIGN text**: the §26 index still titles ADR-0002 "narrowed role", and the §14.6 delta 9 runs into the next paragraph.

</details>

<details>
<summary>Details</summary>

### Pre-consolidation FEAT numbers inside the merged §14

The renumbering changed what FEAT ids mean. At `77667ee`, FEAT-005 was "Telegram deterministic decision paths, the §14.5 command set … and the chat-relay plumbing" and FEAT-009 was "CR-002: research egress …, Hermes tiers". Now FEAT-005 is the Phase 1 rework and FEAT-009 is Telegram workspace M1. CR-003 was merged "verbatim in substance", so DESIGN still says:

```
§14.20.8  M1 (extends FEAT-005 item 22)          -> item 22 is now "Hard ceiling and size measurement"
§14.20.8  M2 (extends FEAT-005 item 23, FEAT-007) -> item 23 is now "Research egress and egress modes"
§14.20.1  cards.py: "Decorated S1 layout via the kit (FEAT-005 item 22)"
§14.18.4  "the FEAT-009 tier switch"              -> the tier switch is now FEAT-007 item 36
§14.15 / §14.20.2  "FEAT-009's skill_reviews row … whatever id format FEAT-009 ships"
                                                  -> skill_reviews is now written in FEAT-007 item 36
§14.20.2  "pending_input is planned in FEAT-005 item 2 (0002_phase2.sql) … This CR does not edit
           FEAT-005; … that feature is being built concurrently"
§14.9 U14, §14.10.4 rule 5, §14.11.1, §14.18.1/.2, §14.21.1  "FEAT-005's edit_comment prompt/path"
ADR-0019 line 220  "Replace runs FEAT-005's edit_comment path"
```

(confirmed; `git show 77667ee:docs/plan/task-ossbot-v1/features/FEAT-00{5,9}.json` vs HEAD.)

The migration reference creates a concrete collision. New FEAT-005 item 24 creates `0002_decisions.sql`, and no PLAN item mentions `pending_input` or `chat_sessions`. The only remaining spec for those tables is the draft text in `FEAT-009.json` step 2: "add … migrations/0002_phase2.sql (tables pending_input(…), chat_sessions(…))". Following the documents as written gives two `0002_*` migrations, and DESIGN attributes the table to the wrong FEAT. The `edit_comment` path has the same orphaning problem: DESIGN treats it as an existing FEAT-005 service ("Replace calls FEAT-005's `edit_comment` service function, never a copy of it", §14.21.1), but no current PLAN item builds it. Item 45 builds S7 "edit title/body/comment", which is probably the intent.

The fix is mechanical. Either add one line to the §14 preamble that maps old to new FEAT ids, or, better, rewrite each occurrence to a PLAN item number (45 for edit_comment and S7, 36 for skill_reviews and the tier switch, 24 for the decision migration). Then give `pending_input`/`chat_sessions` an owning item and a migration number.

### Threat model and targets schema left at revision 4

Several sections outside the CR merge targets now contradict revision-5 defaults, and two of them are security statements (confirmed):

- **§8.4, T1/T2 key-theft row:** says `agent-llm` "reaches only Kiro hosts". Under the default egress mode `open`, `agent-llm` and `catalog` get `web_public` (§11.5, ADR-0005 rev-5 amendment, CR2-6b). This is the same misstatement P1-18 fixes in the VERIFICATION threat table, but the DESIGN copy is not on any row.
- **§8.4, T6 (compromised Hermes):** lists the surface as `memory`, `todo`, `session_search`, `ossbot` with "`skills` … `web` … disabled twice". The default tier is `self_improve`, which enables `skills` (`skill_manage` is a write tool) behind the skill gate, and `research` may add `web`. §15.1 got a revision-5 note and §15.5 has the tiers, but the threat row that justifies the residual risk does not mention the staging dir, the root hasher or the skill gate.
- **§8.5:** "Telegram rendering and buttons … Hermes text is rendered as escaped text only, never buttons" contradicts D8/D9 (typed-block rendering, follow-up buttons). §14.1 and ADR-0004 were amended; §8.5 was not.
- **§17.1:** the `targets.yaml` schema still has `defaults.max_changed_lines: 400`. CR-004 Amendment B and §12.6 remove the per-target line cap ("no hidden hard size limit below the ceiling"), and PLAN item 22 deletes it from `TargetDefaults`. A builder reading §17.1 as the schema would keep it.

§14.20.8 already lists the cross-section amendments CR-003 needed (T5 got one), so the gap is limited to the CR-002/CR-004 consequences outside §10/§11/§12/§15. Amend T1/T2, T6, §8.5 and §17.1, and add rows (or extend P1-18 and CR4-2o) so the build checks them.

### §14.19 integration hooks have no rows

§14.19 is a decision table of 18 integrations, each tagged v1, v1.x or Phase 2. It answers the user's message 5 ("too many integrations…"). The matrix has no row for the table or its items (confirmed by searching TRACEABILITY.md for clip, release watch, notifications, whisper, calendar, heartbeat, weather, streak, followers, OCR):

- **v1 #1, digest stats** (followers, stars, merges, streak): only the merge-rate-by-size part is traced (CR3-AC-42, CR4-3c).
- **v1 #2 (status card) and #3 (reminders):** reached indirectly through CR3-M3.
- **v1 #4 (food photo):** covered by CR2-4/CR2-7l.
- **v1.x #6–#10** (web clipping, GitHub notifications inbox, release watch, PDF reading, voice transcription): no row of any status. PLAN FEAT-010 is titled "M2–M4 and v1.x" but builds only Telegraph from v1.x. Nothing marks them deferred, so they are silently out of scope. That is the "nothing missed" failure the user asked to avoid (message 11).
- **Phase 2 #12–#14, #17** (image tools, calendar, heartbeat, weather): not among the `DES-DEF-*` rows.

Add one row per §14.19 item with phase `P2 FEAT-0nn` or `deferred`, and make §14.20.8 / PLAN FEAT-010 say explicitly which v1.x items Phase 2 builds.

### PLAN items that close rows assigned to another FEAT

PLAN says "every item below names the rows it closes", and each FEAT's close-out is defined against the matrix ("no FEAT-005 row is left `rework`"). Cross-checking every open row's phase column against the item lists found these disagreements (confirmed by grep):

| Row | Matrix phase | PLAN |
|---|---|---|
| TGRW-9 (`ALLOWED_UPDATES`, `authorized_chat_only()`), status `rework` | P1-rework FEAT-005 | not in any FEAT-005 item; `authorized_chat_only()` is built in FEAT-010 item 49 |
| TGRW-11 (`_send_reply`/`_edit` via `telegram.ui.send`) | P2 FEAT-009 | claimed by FEAT-005 item 25, which does not build `telegram.ui.send` (item 44 does) |
| CR3-U13 (ceiling not changeable from Telegram) | P1-rework FEAT-005 | FEAT-009 item 46; the writer is FEAT-006 item 29 |
| CR3-D10, CR3-V-13 | P2 FEAT-009 | FEAT-010 item 49 |
| CR3-U9 | P2 FEAT-010 | FEAT-009 item 44 |
| CR3-D5, CR3-D6, CR3-U4, DES-FR-14 | FEAT-009 / FEAT-006 | not named by any item |

TGRW-9 is the one that bites. FEAT-005's close-out gate cannot pass while it stays `rework`, and closing it in FEAT-005 would accept a from-less update before the streaming code that needs it exists. Moving it to FEAT-010 is the right answer. The rest are bookkeeping, but the matrix is the stated source of status, so the two have to agree.

### §14.1–§14.5 kept beside §14.6–§14.25

The brief asked for §14 to be replaced by CR-003. Revision 5 instead keeps the revision-4 subsections verbatim with an amendment note on each and the rule "where a baseline rule conflicts with §14.6–§14.25 the later section wins". The reason given (Phase 1 docstrings cite §14.1–§14.5) is real, and the notes are accurate. The cost is that a reader meets two preview-card layouts, two keyboards, two `/approve` semantics and `/newchat` vs `/new`, and has to apply the precedence rule each time. This is not blocking. A cheaper long-term shape is to move §14.1–§14.5 into a "revision-4 baseline (superseded)" appendix, keep only the anchors the docstrings need, and update those docstrings in FEAT-009.

### Weak cells in the matrix

21 `rework` rows have evidence `–`: DES-FR-18, DES-FR-21, CR2-1b, CR3-U3, CR3-U14, CR3-AC-15/29/31/46/47/57/58/59/66/67, CR3-BA-1…5 and BA-21. One `done` row, CR3-CF-2, is in the same state. For `rework`, the evidence column is where the current non-conforming code is named. Most of these have a matching TGRW row that already says it, so a cross-reference would do (confirmed).

The other weak cells:

- **Unproven `done`:** CR3-S4 ("no inbound exposure") is `done` with "no inbound component exists". Its proving test is still `missing` in DES-AC-42/CHAT-33/CR3-CF-1, which breaks the matrix's own definition of `done`.
- **Copied locations:** S1–S4 all share the location "approvals.py, handlers.py".
- **Non-test test cells:** for P1-15, P1-16 and P1-18 the tests column names `config/egress.yaml`, `scripts/hyperv-firewall.ps1` and "review".
- **Placeholder pointers:** about 120 rows use "see §14.20.1", "telegram/** (§14.20.1)" or "§14.21 ACs". That is acceptable for `missing` rows today, but FEAT-011's final check needs real locations, so each closing commit has to replace them.

### Carried planner-draft text in the FEAT JSON files

FEAT-005…009 steps end with "Carried detail from the interrupted planner draft (FEAT numbers remapped; DESIGN revision 5 and the item text above win on any conflict…)". The precedence rule is stated, so this is acceptable as history. Two parts are still a problem. The block is the only spec for `pending_input`/`chat_sessions` (see the first section). FEAT-009 draft item 3 restates the revision-4 `/approve` N4 semantics that §14.18.4 deltas 5 and 7 replace. Prune or rewrite the conflicting parts so that "the item text wins" never has to break a tie.

Two smaller things: the §26 ADR index still titles ADR-0002 "Hermes (narrowed role)", although its revision-5 amendment widens that role to the general assistant, and DESIGN §14.6's numbered delta list runs item 9 into the next paragraph.

</details>

<details>
<summary>File map</summary>

- `docs/DESIGN.md`: revision 5. CR-003 as §14.6–§14.25 with the section map; amendment notes on §14.1–§14.5, §15.1 and §8.4 T5; new §9.5a/§9.5b/§9.14, §11.5, §13.5, §15.5, §15.6, §22.7b, FR-17…22, AC 31–42; §25 decisions.
- `docs/TRACEABILITY.md`: new, 488 rows plus the Phase 1 code audit (TGRW-*).
- `docs/PLAN.md`: Phase 1 marked done; new "Phase 1 rework (FEAT-005)"; Phase 2 rewritten as FEAT-006…011; decisions 13–16; verification bar items 9–10.
- `docs/adr/0002,0003,0004,0005,0008,0009,0018`: revision-5 amendment blocks. `0019`, `0020`: status Accepted.
- `docs/plan/CR-00{1..4}*.md`, `NOTE-live-secrets.md`: "Merged into DESIGN.md §…, historical" headers. CR-003 is cut to its review responses.
- `docs/design-review.md`, `docs/ux/cr003-review.md`, `docs/ux/cr003-carried-findings.md`: historical/recorded headers.
- `docs/plan/task-ossbot-v1/{task,context}.json`, `features/FEAT-005…011.json`: regenerated for the new plan. FEAT-010 and FEAT-011 are new.
- `docs/VERIFICATION.md`: 5-line note.

Full diff: `git show b0402b4`.

</details>
