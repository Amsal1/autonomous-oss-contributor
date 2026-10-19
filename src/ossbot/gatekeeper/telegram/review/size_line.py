"""The S1 size line and the overrun formula (DESIGN §14.11.1, CR-004 Amendment B; AC 39, AC 54).

Gatekeeper facts from ``packaging.size_report`` (pinned numstat split by the size globs; test lines are
shown separately and never count toward the class). The estimate is the one bound by the approved
proposal and is never re-estimated here. The ⚠️ highlight is a fact plus a labelled 🤖 claim, never a gate.

* ``code_changed = added_code + deleted_code``.
* ``overrun_pct = round(100 * (code_changed - est_code) / est_code)`` when ``est_code >= 1``; with
  ``est_code == 0`` the line reads ``est 0 code`` instead of a percentage.
* The ⚠️ appears when ``code_changed > est_code * (1 + tolerance)`` or the measured class is larger
  than the estimated one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ossbot.common.size import SIZE_CLASSES
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui.limits import middle_cut

REASON_MAX = 120
OUTSIDE_SHOWN = 3


@dataclass(frozen=True)
class SizeFacts:
    lines: tuple[k.Line, ...]
    flagged: bool
    overrun_pct: int | None


def overrun_pct(code_changed: int, est_code: int) -> int | None:
    if est_code < 1:
        return None
    return round(100 * (code_changed - est_code) / est_code)


def size_lines(report: Mapping[str, Any] | None, *, coder_reason: str | None = None) -> SizeFacts:
    """The required size block: one line, plus the ⚠️ lines when flagged."""
    if not report:
        return SizeFacts((k.line("Size ? · (no measurement)"),), False, None)
    m = report.get("measured") or {}
    added, deleted = int(m.get("code_added", 0)), int(m.get("code_deleted", 0))
    test = int(m.get("test_added", 0)) + int(m.get("test_deleted", 0))
    files = int(m.get("files", 0))
    measured = str(report.get("measured_class") or "?")
    actual = f"+{added} / −{deleted} code · +{test} test · {files} files"
    estimate = report.get("estimate")
    if not estimate:  # push_update, comment or an item from before CR-004: no proposal estimate
        return SizeFacts((k.line(f"Size {measured} · {actual}"),), False, None)
    est_code = int(estimate.get("code_lines", 0))
    approved = str(report.get("approved_class") or measured)
    code_changed = added + deleted
    tolerance = float(report.get("tolerance", 0.5))
    grew = measured in SIZE_CLASSES and approved in SIZE_CLASSES and (
        SIZE_CLASSES.index(measured) > SIZE_CLASSES.index(approved))  # fmt: skip
    over = code_changed > est_code * (1 + tolerance)
    flagged = grew or over
    badge = f"⚠️ {measured} (est {approved})" if flagged else f"✅ {measured}"
    lines: list[k.Line] = [k.line(f"Size {badge} · est ~{est_code} code → actual {actual}")]
    pct = overrun_pct(code_changed, est_code)
    if flagged:
        reason = (coder_reason or "").strip()
        claim: list[k.Span] = []
        if reason:
            claim = [k.Const(' · 🤖 coder: "'), k.Text(reason, REASON_MAX), k.Const('"')]
        if pct is None:
            head: k.Span = k.Bold(k.Const("est 0 code"))
            lines.append((k.Const("⚠️ "), head, k.Const(f" · actual +{added} / −{deleted} code"), *claim))
        else:
            lines.append((k.Const("⚠️ "), k.Bold(k.Const(f"{pct:+d}% over estimate")), *claim))
    outside = [str(p) for p in report.get("outside_areas") or []]
    if outside:
        shown = ", ".join(middle_cut(p, 40) for p in outside[:OUTSIDE_SHOWN])
        more = f" +{len(outside) - OUTSIDE_SHOWN}" if len(outside) > OUTSIDE_SHOWN else ""
        lines.append((k.Const(f"⚠️ {len(outside)} files outside the plan: "), k.Text(shown + more, 200)))
    return SizeFacts(tuple(lines), flagged, pct)
