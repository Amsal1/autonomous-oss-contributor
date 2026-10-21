"""Scout (CR-004; DESIGN §9.5a): shallow read-only snapshot + the ``scout`` role -> ``Proposal``.

Enters SCOUTING from VETTED, from PROPOSAL_SNOOZED (snooze over) or PROPOSAL_EXPIRED (re-scout
cooldown over), and re-scouts in place for a pending Adjust / Make it smaller / Split (PROPOSED or
RESCOPE_REQUIRED). A snoozed or expired item is re-scouted only while it still applies (issue
open, unassigned, unlocked, no open PR mentioning it, repo not set to "never"); otherwise it is
SKIPPED with the reason. Autonomous first proposals respect ``proposal.max_per_day`` before any
work: while the gatekeeper's pacing says no slot is left, the item waits (``CapReached``) instead
of scouting into a 429. The clone is a ``fetch`` job with ``prefetch: false`` (depth 1, single branch,
no tags, no submodules, no LFS, https only, no hooks, no credentials; nothing from the repo runs);
the scout session reads it as a read-only snapshot in the ``research`` profile (no shell). A clone
over ``SCOUT_MAX_MB`` skips the item (``snapshot_too_large``, with a note). The task volume is
destroyed when the stage ends, whatever the outcome, so gate 1 waits without one; the real fetch
after Start work clones afresh (DESIGN §9.5a, ADR-0005 addendum). A re-scout from RESCOPE_REQUIRED (work
has started) clones nothing: it reads the build tree of the earlier attempt as its snapshot, keeps
the volume if it fails (Continue still needs it) and, once the new version is stored, destroys it
and clears the runner's build notes, so the next Start work fetches, plans and packages afresh.
Its ``fetch.json`` (for a rescope, the build's) says whether the repo has a
test suite; a proposal listing no tests for such a repo gets one repair retry. The gatekeeper
validates and stores the proposal and moves the item to PROPOSED; autonomous proposals above
``autonomous_max_class``, an excluded kind or a "never" repo are refused there and the item is
SKIPPED here. 👤 items have no size preference (the ceiling still applies).
"""

from __future__ import annotations

import json
from typing import Any

from ossbot.common.clock import from_iso
from ossbot.common.schemas import Proposal
from ossbot.runner import prompts
from ossbot.runner.errors import CapReached, StageFailed
from ossbot.runner.gk_client import GkError
from ossbot.runner.outputs import SCHEMA_HINTS
from ossbot.runner.pipeline import StageContext, StageRun
from ossbot.runner.stages.common import (
    artifact_json,
    destroy_volume,
    has_test_suite,
    issue_text,
    job,
    owner_notes,
    target,
)

STAGE = "scout"
SCOUT_MAX_MB = 2048  # sandbox/entrypoints/fetch.sh SCOUT_MAX_MB (error_class snapshot_too_large)
SKIP_CODES = {"above_autonomous_max": "too_big_for_auto", "kind_filtered": "kind_filtered",
              "never_repo": "never_repo", "above_ceiling": "above_ceiling"}  # fmt: skip
# runner notes of a build attempt, cleared when a rescope ends in a new proposal version
BUILD_NOTES = ("fetch", "fetch_artifacts", "implement", "validation", "diff", "review", "package_body", "packaged",
               "watch_fix")  # fmt: skip
TESTS_REQUIRED = (
    "the repository has a test suite: `tests` must list the tests to add or update (leave `no_suite_reason` null)"
)


def _prs(sc: StageContext, repo: str) -> tuple[str, str]:
    merged = [p for p in sc.gh.pulls(repo, "closed", 30) if p.get("merged_at")][:10]
    open_ = sc.gh.pulls(repo, "open", 30)
    fmt = "\n".join(
        f"- #{p.get('number')} {p.get('title', '')} (by {(p.get('user') or {}).get('login', '?')},"
        f" merged {p.get('merged_at')})" for p in merged
    )  # fmt: skip
    fmt_open = "\n".join(f"- #{p.get('number')} {p.get('title', '')}" for p in open_)
    return (prompts.untrusted(f"github:{repo} recent merged PRs", fmt or "(none)"),
            prompts.untrusted(f"github:{repo} open PRs", fmt_open or "(none)"))  # fmt: skip


def not_relevant(sc: StageContext, c: dict[str, Any]) -> str | None:
    """Why a snoozed or expired item no longer applies (None = re-scout it). Reads only."""
    repo, number = str(c["upstream"]), c.get("issue_number")
    if c.get("source") != "user" and sc.gk.prefs(repo).get("never_repo"):
        return "never_repo"
    if number is None:
        return None  # repo- or PR-level 👤 work: nothing to re-read
    issue = sc.gh.issue(repo, int(number))
    if issue is None:
        return "issue_gone"
    if issue.get("state") != "open":
        return "issue_closed"
    if issue.get("assignee") or issue.get("assignees"):
        return "issue_assigned"
    if issue.get("locked"):
        return "issue_locked"
    if sc.gh.open_prs_mentioning(repo, int(number)) > 0:
        return "linked_open_pr"
    return None


def _hold_for_cap(sc: StageContext, c: dict[str, Any]) -> None:
    """Autonomous first proposals only (revisions and 👤 items are not counted, §9.5b)."""
    if c.get("source") == "user" or c.get("proposal_version") is not None:
        return
    pacing = sc.gk.pacing()
    if int(pacing.get("remaining", 1)) <= 0:
        until = pacing.get("next_slot_at")
        raise CapReached(from_iso(str(until)) if until else None)


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid, repo, state = str(c["id"]), str(c["upstream"]), str(c["state"])
    _hold_for_cap(sc, c)
    if state in {"PROPOSAL_SNOOZED", "PROPOSAL_EXPIRED"}:
        reason = not_relevant(sc, c)
        if reason is not None:
            if state == "PROPOSAL_SNOOZED":  # no direct edge: through SCOUTING, nothing is run
                sc.transition(cid, state, "SCOUTING")
                state = "SCOUTING"
            sc.transition(cid, state, "SKIPPED", {"reason": f"no_longer_relevant:{reason}"})
            return
    if state in {"VETTED", "PROPOSAL_SNOOZED", "PROPOSAL_EXPIRED"}:
        sc.transition(cid, state, "SCOUTING")
        state = "SCOUTING"
    user = c.get("source") == "user"
    keep = state == "RESCOPE_REQUIRED"  # work has started: the volume holds the build tree
    try:
        skip = _scout(sc, c, state, user)
        keep = False  # a new version (PROPOSED) or SKIPPED: the old build tree is not needed any more
    finally:  # the scout clone never outlives the stage: gate 1 waits without a task volume
        if not keep:  # still RESCOPE_REQUIRED after a failure: Continue / a retry need the build tree
            destroy_volume(sc, cid)
    if state == "RESCOPE_REQUIRED":  # the next Start work begins a new build (fresh fetch, rev notes cleared)
        sc.save(cid, **dict.fromkeys(BUILD_NOTES))
    if skip is None:
        return
    sc.transition(cid, "SCOUTING", "SKIPPED", {"reason": skip})
    where = f"{repo}#{c['issue_number']}" if c.get("issue_number") else repo
    if skip == "snapshot_too_large":
        noted = sc.load("_repos").get("too_large_noted", [])
        if user or repo not in noted:  # autonomous: one note per repo
            sc.save("_repos", too_large_noted=sorted({*noted, repo}))
            sc.say(f"🛑 {where}: skipped, the repository is too large to scout (its shallow clone is over"
                   f" {SCOUT_MAX_MB} MB). Nothing was started.")  # fmt: skip
    elif user:
        sc.say(f"🛑 {where}: the scouted change is {skip.replace('_', ' ')}; nothing was started.")


def _scout(sc: StageContext, c: dict[str, Any], state: str, user: bool) -> str | None:
    """The stage proper; returns a skip reason, or None once the proposal is stored."""
    cid, repo = str(c["id"]), str(c["upstream"])
    feedback = json.loads(c["proposal_feedback_json"]) if c.get("proposal_feedback_json") else None
    with StageRun(sc, cid, STAGE, profile="research") as run_:
        rescope = state == "RESCOPE_REQUIRED"
        if rescope:  # the snapshot is the build tree of the earlier attempt: no second clone into it
            suite = has_test_suite(sc.load(cid).get("fetch"))
        else:
            depth_args = {"repo": repo, "clone_depth": 1, "prefetch": False}
            try:
                clone = job(sc, cid, STAGE, "fetch", "fetch", depth_args, "rw", egress=target(sc, repo).egress)
            except StageFailed as exc:
                if exc.error_class != "snapshot_too_large" or state != "SCOUTING":
                    raise
                return "snapshot_too_large"
            suite = has_test_suite(artifact_json(clone, "fetch.json"))
        issue, comments = issue_text(sc, c)
        merged, open_prs = _prs(sc, repo)
        if user:
            budget = "Owner-directed work (👤): no size preference; stay under the hard ceiling of the policy."
        else:
            prefs = sc.gk.prefs(repo)
            budget = (f"Autonomous work: the largest class you may propose is {prefs['autonomous_max_class']};"
                      f" preferred classes: {', '.join(prefs['preferred_classes'])}. Propose a reduced scope that"
                      " fits, and the full version as an alternative.")  # fmt: skip
        sections = [issue, comments, merged, open_prs, *owner_notes(c), f"## Size budget\n\n{budget}"]
        if suite:
            sections.append("## Tests\n\nThe repository has a test suite: list the tests you will add or update.")
        if c.get("vetting_json"):
            sections.append(prompts.untrusted("vet facts", str(c["vetting_json"])))
        if rescope:
            sections.append("## Snapshot\n\nThe snapshot is the working tree of the earlier attempt: it already"
                            " contains the changes made for the approved version. Propose against the upstream"
                            " base; the new version starts from a fresh clone.")  # fmt: skip
        if feedback is not None:
            sections.append(prompts.human(f"Revise proposal v{feedback['version']}: {feedback['text']}"))
        text = prompts.task("scout", "Study the repository snapshot and propose one change for approval.",
                            sections, SCHEMA_HINTS["scout"])  # fmt: skip

        def tests_listed(p: Proposal) -> str | None:
            return TESTS_REQUIRED if suite and not p.tests else None

        result = sc.kiro().invoke(cid, "scout", Proposal, text, timeout_s=1200, schema_hint=SCHEMA_HINTS["scout"],
                                  check=tests_listed)  # fmt: skip
        run_.model_actual = result.model_actual
        revises = int(c["proposal_version"]) if state != "SCOUTING" and c.get("proposal_version") else None
        try:
            sc.gk.submit_proposal(cid, result.value.model_dump(by_alias=True), revises,
                                  feedback["text"] if feedback else None)  # fmt: skip
        except GkError as exc:
            if exc.status == 429:
                detail = exc.detail.get("detail") if isinstance(exc.detail, dict) else None
                try:
                    until = from_iso(str(detail)) if detail else None
                except ValueError:
                    until = None
                raise CapReached(until) from exc
            if exc.status == 422 and exc.code in SKIP_CODES and state == "SCOUTING":
                return SKIP_CODES[exc.code]
            raise
    return None
