"""Runner stages (DESIGN §9.1). ``STAGES`` covers every name in ``STAGE_ENTRYPOINTS``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ossbot.runner.pipeline import StageContext
from ossbot.runner.stages import (
    catalog,
    discover,
    fetch,
    implement,
    importer,
    package,
    plan,
    review,
    scout,
    validate,
    vet,
    watch,
)

ContributionStage = Callable[[StageContext, dict[str, Any]], None]

# contribution state -> stage that moves it on (DESIGN §9.2); waiting states are absent
BY_STATE: dict[str, ContributionStage] = {
    "DISCOVERED": vet.run, "VETTING": vet.run,
    "VETTED": scout.run, "SCOUTING": scout.run,
    "PROPOSAL_APPROVED": fetch.run, "IMPORTED": fetch.run, "FETCHING": fetch.run,
    "PLANNING": plan.run,
    "PLANNED": implement.run, "IMPLEMENTING": implement.run, "VALIDATION_FAILED": implement.run,
    "REVISE": implement.run, "CHANGES_REQUESTED": implement.run,
    "IMPLEMENTED": validate.run, "VALIDATING": validate.run,
    "VALIDATED": review.run, "REVIEWING": review.run,
    "REVIEW_PASSED": package.run, "PACKAGING": package.run,
    "WATCHING": watch.run,
}  # fmt: skip
# every runner stage name (shared with STAGE_ENTRYPOINTS and the gatekeeper's stage-run gate)
STAGES: dict[str, object] = {
    "discover": discover.run, "vet": vet.run, "scout": scout.run, "fetch": fetch.run, "plan": plan.run,
    "implement": implement.run, "validate": validate.run, "review": review.run, "package": package.run,
    "watch": watch.run, "catalog": catalog.run, "import": importer.run,
}  # fmt: skip


def stage_for(c: dict[str, Any]) -> ContributionStage | None:
    """The stage for a contribution now, or None (waiting, terminal, gate-held)."""
    state = str(c.get("state"))
    if state == "FETCHED":
        return importer.run if c.get("source") == "operator" else plan.run
    if state in {"PROPOSED", "RESCOPE_REQUIRED"}:
        return scout.run if c.get("feedback_pending") or c.get("proposal_feedback_json") else None
    if state in {"PROPOSAL_SNOOZED", "PROPOSAL_EXPIRED"}:
        return scout.run  # the scheduler checks snooze end / re-scout cooldown first
    return BY_STATE.get(state)
