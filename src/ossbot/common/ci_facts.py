"""CI-on-PR facts of a target repo (DESIGN §9.4, §9.5; post-v1 owner rule): stored shape and card wording.

The runner detects them (``runner/ci_detect.py``, GET only, read token) and stores them on the vetting;
discovery uses ``status`` as a ranking boost, never a filter. The gatekeeper renders them only from closed
sets and counts (``display``); check names are repo text, bounded by ``from_stored`` and shown only
through the UI kit's escaped ``Text``.

``status``: ``yes`` = most sampled merged PRs show test/lint/build checks that ran; ``no`` = merged PRs
without any check and no PR-triggered workflow; ``unknown`` otherwise (workflow files alone, a gated
CI without visible runs, a read error). ``gating``: how CI starts on an outside PR (``auto``,
``approval_required`` by a maintainer, ``label_or_comment`` such as ``ok-to-test``, ``unknown``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

CiStatus = Literal["yes", "no", "unknown"]
CiGating = Literal["auto", "approval_required", "label_or_comment", "unknown"]
CiSource = Literal["merged_prs", "workflows", "none"]
CI_STATUSES: tuple[CiStatus, ...] = ("yes", "no", "unknown")
CI_GATINGS: tuple[CiGating, ...] = ("auto", "approval_required", "label_or_comment", "unknown")
CI_SOURCES: tuple[CiSource, ...] = ("merged_prs", "workflows", "none")
CI_KINDS: tuple[str, ...] = ("tests", "lint", "build")
GATED: frozenset[str] = frozenset({"approval_required", "label_or_comment"})
CHECKS_MAX, CHECK_NAME_MAX, SAMPLE_MAX = 8, 40, 50
WAITS_FOR_APPROVAL = "⏳ CI waits for maintainer approval"
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


@dataclass(frozen=True)
class CiFacts:
    status: CiStatus = "unknown"
    kinds: tuple[str, ...] = ()  # ⊆ CI_KINDS, in that order
    checks: tuple[str, ...] = ()  # ≤ CHECKS_MAX names, ≤ CHECK_NAME_MAX chars (repo text)
    sampled: int = 0  # merged PRs sampled
    with_checks: int = 0  # of them, with a test/lint/build check that ran
    gating: CiGating = "unknown"
    source: CiSource = "none"

    def stored(self) -> dict[str, Any]:
        """The vetting fields (``vetting_json``) and the runner's cache entry."""
        return {"ci_on_pr": self.status, "ci_kinds": list(self.kinds), "ci_checks": list(self.checks),
                "ci_sampled": self.sampled, "ci_with_checks": self.with_checks, "ci_gating": self.gating,
                "ci_source": self.source}  # fmt: skip


def check_name(name: object) -> str:
    """A check name bounded for storage: no control or bidi characters, ≤ ``CHECK_NAME_MAX``."""
    return _CONTROL.sub("", str(name)).strip()[:CHECK_NAME_MAX]


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= SAMPLE_MAX else 0


def from_stored(doc: Mapping[str, Any]) -> CiFacts:
    """Stored vetting fields -> facts; anything outside the closed sets reads as unknown/dropped
    (a legacy row without CI keys is ``unknown``)."""
    status = doc.get("ci_on_pr")
    gating = doc.get("ci_gating")
    source = doc.get("ci_source")
    raw_kinds = doc.get("ci_kinds")
    kinds = set(raw_kinds) if isinstance(raw_kinds, list) else set()
    raw_checks = doc.get("ci_checks")
    checks = [check_name(c) for c in raw_checks if isinstance(c, str)] if isinstance(raw_checks, list) else []
    sampled = _count(doc.get("ci_sampled"))
    return CiFacts(
        status=status if status in CI_STATUSES else "unknown",
        kinds=tuple(k for k in CI_KINDS if k in kinds),
        checks=tuple(c for c in checks if c)[:CHECKS_MAX],
        sampled=sampled,
        with_checks=min(_count(doc.get("ci_with_checks")), sampled),
        gating=gating if gating in CI_GATINGS else "unknown",
        source=source if source in CI_SOURCES else "none",
    )


def display(facts: CiFacts) -> str:
    """``✅ tests, lint (8/10 merged PRs) · runs after maintainer approval`` (closed sets and counts only)."""
    if facts.status == "yes":
        text = "✅ " + (", ".join(facts.kinds) or "checks")
        if facts.sampled:
            text += f" ({facts.with_checks}/{facts.sampled} merged PRs)"
    elif facts.status == "no":
        text = "❌ none found"
    else:
        text = "❔ unknown" + (" (workflow files only)" if facts.source == "workflows" else "")
    if facts.gating == "approval_required":
        text += " · runs after maintainer approval"
    elif facts.gating == "label_or_comment":
        text += " · runs after a maintainer label or /ok-to-test"
    return text
