"""Discovery ranking and filters (DESIGN §9.4, §9.14): autonomous only, smallest class first."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ossbot.common.config import DiscoveryFilters
from ossbot.runner.scoring import Candidate, RepoSignals, estimate_class, filter_reason, kind_of, rank

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def cand(number: int, title: str, labels: tuple[str, ...] = ("bug",), body: str = "", days: int = 2) -> Candidate:
    return Candidate("o/r", number, title, body, labels, NOW - timedelta(days=days), NOW - timedelta(days=30))


def test_prefers_the_smallest_class() -> None:
    small = cand(1, "Fix typo in README", ("documentation",))
    medium = cand(2, "Crash when spans are empty", ("bug",))
    big = cand(3, "Redesign the exporter", ("bug",))
    kept, dropped = rank([big, medium, small], filters=DiscoveryFilters(), max_class="m", preferred=["xs", "s"],
                         signals={}, now=NOW)  # fmt: skip
    assert [r.candidate.number for r in kept] == [1, 2, 3]
    assert [r.est_class for r in kept] == ["xs", "s", "l"]
    assert dropped == {}


def test_clearly_above_the_max_is_dropped() -> None:
    big = cand(3, "Redesign the exporter")
    kept, dropped = rank([big], filters=DiscoveryFilters(), max_class="xs", preferred=["xs"], signals={}, now=NOW)
    assert kept == [] and dropped == {"o/r#3": "too_big:l"}


def test_filters() -> None:
    f = DiscoveryFilters(require_labels_any=["good first issue"], max_issue_age_days=10)
    c = cand(1, "Crash", ("bug",))
    assert filter_reason(c, f, "s", NOW, RepoSignals(), est_class="s", kind="bug") == "labels"
    old = cand(2, "Crash", ("good first issue",), days=40)
    assert filter_reason(old, f, "s", NOW, RepoSignals(), est_class="s", kind="bug") == "stale_issue"
    dep = cand(3, "Add a dependency", ("dependencies",))
    assert kind_of(dep.labels, dep.title) == "new-dependency"
    assert filter_reason(dep, DiscoveryFilters(), "s", NOW, RepoSignals(), est_class="s",
                         kind="new-dependency") == "kind_excluded:new-dependency"  # fmt: skip
    quiet = RepoSignals(last_maintainer_activity=NOW - timedelta(days=90))
    assert filter_reason(c, DiscoveryFilters(), "s", NOW, quiet, est_class="s", kind="bug") == "inactive_maintainers"


def test_signals_raise_the_score() -> None:
    a, b = cand(1, "Crash A"), cand(2, "Crash B")
    signals = {"o/r": RepoSignals(median_merge_hours=12, outside_merge_rate=0.8, owner_merged=2)}
    kept, _ = rank([a, b], filters=DiscoveryFilters(), max_class="s", preferred=["s"], signals=signals, now=NOW)
    assert kept[0].reasons["responsiveness"] > 9 and kept[0].reasons["merge_rate"] == 8.0
    assert estimate_class(cand(9, "x", body="y" * 7000)) == "m"
