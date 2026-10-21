"""CI-on-PR facts: stored shape and the card wording (closed sets and counts only)."""

from __future__ import annotations

from ossbot.common.ci_facts import CiFacts, display, from_stored


def test_display() -> None:
    yes = CiFacts("yes", ("tests", "lint"), ("pytest",), 10, 8, "approval_required", "merged_prs")
    assert display(yes) == "✅ tests, lint (8/10 merged PRs) · runs after maintainer approval"
    assert display(CiFacts("yes", (), (), 3, 2, "auto", "merged_prs")) == "✅ checks (2/3 merged PRs)"
    assert display(CiFacts("no", source="merged_prs")) == "❌ none found"
    assert display(CiFacts()) == "❔ unknown"
    gated = CiFacts("unknown", ("tests",), (), 0, 0, "label_or_comment", "workflows")
    assert display(gated) == "❔ unknown (workflow files only) · runs after a maintainer label or /ok-to-test"


def test_round_trip_and_hostile_values() -> None:
    facts = CiFacts("yes", ("tests", "build"), ("unit", "e2e"), 5, 4, "auto", "merged_prs")
    assert from_stored(facts.stored()) == facts
    assert from_stored({}) == CiFacts()  # a legacy vetting without CI keys
    hostile = {"ci_on_pr": "<b>yes</b>", "ci_kinds": ["tests", "<script>"], "ci_checks": ["a\u202eb" + "x" * 90, 7],
               "ci_sampled": 10**6, "ci_with_checks": True, "ci_gating": "always", "ci_source": "llm"}  # fmt: skip
    got = from_stored(hostile)
    assert (got.status, got.kinds, got.sampled, got.with_checks, got.gating, got.source) == (
        "unknown", ("tests",), 0, 0, "unknown", "none")  # fmt: skip
    assert got.checks == (("ab" + "x" * 90)[:40],)
    assert from_stored({"ci_sampled": 3, "ci_with_checks": 9}).with_checks == 3
