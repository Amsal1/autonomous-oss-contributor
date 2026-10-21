"""Ranking and filters for autonomous discovery only (DESIGN §9.4, §9.14; CR-004 Amendment B).

User-directed (👤) work never passes through here: it skips ranking, caps, the size preference and
the filters. Size estimates from issue text are coarse; the scout's estimate is the real gate, so a
candidate is dropped here only when it is *clearly* above the autonomous maximum (two classes).
Rank score prefers the smallest class that solves the issue, then maintainer responsiveness,
outside merge rate, inviting labels, freshness and clarity, the owner's interests and history, and a
repo whose CI ran tests/lint on merged PRs (``ci_on_pr_weight``; a boost, never a filter).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from ossbot.common.config import DiscoveryFilters
from ossbot.common.size import SIZE_CLASSES

LABEL_WEIGHT = {"good first issue": 8, "help wanted": 6, "bug": 4, "documentation": 3, "docs": 3}
KIND_BY_LABEL: tuple[tuple[str, str], ...] = (
    ("breaking", "breaking-change"), ("security", "security-sensitive-crypto"), ("crypto", "security-sensitive-crypto"),
    ("dependenc", "new-dependency"), ("api", "api-redesign"), ("doc", "docs"), ("test", "tests"),
    ("ci", "ci-fix"), ("refactor", "refactor-small"), ("bug", "bug"), ("enhancement", "small-feature"),
    ("feature", "small-feature"),
)  # fmt: skip
BIG_WORDS = re.compile(r"\b(refactor|redesign|rewrite|rfc|architecture|migrate|overhaul|breaking)\b", re.IGNORECASE)
SMALL_WORDS = re.compile(r"\b(typo|spelling|broken link|docstring|readme|wording|small|minor|one[- ]line)\b", re.I)
CLARITY = re.compile(r"(steps to reproduce|expected (behaviou?r|result)|actual (behaviou?r|result)|```)", re.I)
CLEARLY_ABOVE = 2  # classes above autonomous_max_class before discovery drops a candidate


@dataclass(frozen=True)
class Candidate:
    repo: str
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    updated_at: datetime
    created_at: datetime
    comments: int = 0
    priority: int = 3
    category: str = ""
    language: str | None = None
    # post-v1 open discovery (design §5.3; review-3 L3): 0.5 * the open repo's score (CI included), so
    # CI counts once for an open candidate; seed repos get only the default-branch activity boost here
    repo_bonus: float = 0.0


@dataclass(frozen=True)
class RepoSignals:
    """Cached repo facts (vet / repo_facts): None = unknown (neutral score)."""

    median_merge_hours: float | None = None
    outside_merge_rate: float | None = None  # 0..1
    last_maintainer_activity: datetime | None = None
    owner_merged: int = 0  # the owner's merged PRs here (outcomes)
    owner_closed_recent: bool = False  # closed unmerged within the cooldown
    ci_on_pr: Literal["yes", "no", "unknown"] = "unknown"  # ci_detect: CI ran tests/lint on merged PRs


@dataclass(frozen=True)
class Ranked:
    candidate: Candidate
    score: float
    est_class: str
    kind: str
    reasons: dict[str, float] = field(default_factory=dict)


def kind_of(labels: Iterable[str], title: str = "") -> str:
    text = " ".join(labels).lower() + " " + title.lower()
    for needle, kind in KIND_BY_LABEL:
        if re.search(rf"\b{re.escape(needle)}", text):
            return kind
    return "bug"


def estimate_class(c: Candidate) -> str:
    """Coarse estimate from issue text and labels: xs/s for small wording, l for big words."""
    labels = {lbl.lower() for lbl in c.labels}
    idx = 1  # s
    if SMALL_WORDS.search(c.title) or "documentation" in labels or "good first issue" in labels:
        idx = 0
    if BIG_WORDS.search(c.title) or BIG_WORDS.search(c.body[:4000]):
        idx = max(idx, 3)
    if len(c.body) > 6000:
        idx = max(idx, 2)
    return SIZE_CLASSES[idx]


def filter_reason(
    c: Candidate, filters: DiscoveryFilters, max_class: str, now: datetime, signals: RepoSignals,
    *, est_class: str, kind: str,
) -> str | None:  # fmt: skip
    """Why an autonomous candidate is dropped (None = kept)."""
    if kind in filters.excluded_kinds:
        return f"kind_excluded:{kind}"
    if filters.allowed_kinds and kind not in filters.allowed_kinds:
        return f"kind_not_allowed:{kind}"
    labels = {lbl.lower() for lbl in c.labels}
    if filters.require_labels_any and not labels & {lbl.lower() for lbl in filters.require_labels_any}:
        return "labels"
    if now - c.updated_at > timedelta(days=filters.max_issue_age_days):
        return "stale_issue"
    activity = signals.last_maintainer_activity
    if activity is not None and now - activity > timedelta(days=filters.min_maintainer_activity_days):
        return "inactive_maintainers"
    if (
        filters.languages
        and c.language is not None
        and c.language.lower() not in {lang.lower() for lang in filters.languages}
    ):
        return "language"
    if SIZE_CLASSES.index(est_class) >= SIZE_CLASSES.index(max_class) + CLEARLY_ABOVE:  # type: ignore[arg-type]
        return f"too_big:{est_class}"
    return None


def score(
    c: Candidate, signals: RepoSignals, now: datetime, *, est_class: str, preferred: Sequence[str],
    interests: Iterable[str] = (), ci_weight: float = 0.0,
) -> tuple[float, dict[str, float]]:  # fmt: skip
    """``ci_weight`` (``policy.discovery.ci_on_pr_weight``) is added only when CI ran on merged PRs
    (``ci_on_pr == "yes"``, gated or not): a boost, never a filter or a penalty."""
    idx = SIZE_CLASSES.index(est_class)  # type: ignore[arg-type]
    parts: dict[str, float] = {
        "priority": 10.0 * (6 - max(1, min(5, c.priority))),
        "size_fit": 12.0 - 2.0 * idx + (4.0 if est_class in preferred else 0.0),
        "labels": float(max((LABEL_WEIGHT.get(lbl.lower(), 0) for lbl in c.labels), default=0)),
        "freshness": 5.0 if now - c.updated_at <= timedelta(days=30) else 0.0,
        "clarity": 3.0 if CLARITY.search(c.body[:8000]) else 0.0,
        "interests": 4.0 if c.category and c.category in set(interests) else 0.0,
    }
    if signals.median_merge_hours is not None:
        parts["responsiveness"] = max(0.0, 10.0 - signals.median_merge_hours / 24.0)
    if signals.outside_merge_rate is not None:
        parts["merge_rate"] = 10.0 * max(0.0, min(1.0, signals.outside_merge_rate))
    parts["history"] = min(5.0, 2.0 * signals.owner_merged) - (10.0 if signals.owner_closed_recent else 0.0)
    if signals.ci_on_pr == "yes" and ci_weight > 0:
        parts["ci_on_pr"] = ci_weight
    if c.comments > 30:
        parts["crowded"] = -10.0
    if c.repo_bonus:
        parts["repo"] = c.repo_bonus
    return sum(parts.values()), parts


def rank(
    candidates: Iterable[Candidate], *, filters: DiscoveryFilters, max_class: str, preferred: Sequence[str],
    signals: dict[str, RepoSignals], now: datetime, interests: Iterable[str] = (), ci_weight: float = 0.0,
) -> tuple[list[Ranked], dict[str, str]]:  # fmt: skip
    """Kept candidates best first (ties: smaller class, then older issue), and the drop reasons."""
    kept: list[Ranked] = []
    dropped: dict[str, str] = {}
    for c in candidates:
        est, kind = estimate_class(c), kind_of(c.labels, c.title)
        sig = signals.get(c.repo, RepoSignals())
        reason = filter_reason(c, filters, max_class, now, sig, est_class=est, kind=kind)
        if reason is not None:
            dropped[f"{c.repo}#{c.number}"] = reason
            continue
        total, parts = score(c, sig, now, est_class=est, preferred=preferred, interests=interests,
                             ci_weight=ci_weight)  # fmt: skip
        kept.append(Ranked(c, total, est, kind, parts))
    kept.sort(key=lambda r: (-r.score, SIZE_CLASSES.index(r.est_class), r.candidate.created_at))  # type: ignore[arg-type]
    return kept, dropped


def candidate_from_issue(repo: str, issue: dict[str, Any], *, priority: int, category: str,
                         language: str | None, repo_bonus: float = 0.0) -> Candidate:  # fmt: skip
    from ossbot.common.clock import from_iso  # noqa: PLC0415

    return Candidate(
        repo_bonus=repo_bonus,
        repo=repo, number=int(issue["number"]), title=str(issue.get("title") or "")[:300],
        body=str(issue.get("body") or "")[:20_000],
        labels=tuple(str(lbl.get("name", "")) for lbl in issue.get("labels") or [] if isinstance(lbl, dict)),
        updated_at=from_iso(str(issue["updated_at"])), created_at=from_iso(str(issue["created_at"])),
        comments=int(issue.get("comments") or 0), priority=priority, category=category, language=language,
    )  # fmt: skip
