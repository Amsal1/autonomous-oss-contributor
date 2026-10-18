"""Scout (CR-004; DESIGN §9.5a): shallow read-only snapshot + the ``scout`` role -> ``Proposal``.

Enters SCOUTING from VETTED, from PROPOSAL_SNOOZED (snooze over) or PROPOSAL_EXPIRED (still
relevant), and re-scouts in place for a pending Adjust / Make it smaller / Split (PROPOSED or
RESCOPE_REQUIRED). The clone is a ``fetch`` job with ``prefetch: false`` and depth 1, so no repo code
runs; the real fetch after Start work replaces it. The gatekeeper validates and stores the
proposal and moves the item to PROPOSED; autonomous proposals above ``autonomous_max_class``, an
excluded kind or a "never" repo are refused there and the item is SKIPPED here. 👤 items have no
size preference (the ceiling still applies).
"""

from __future__ import annotations

import json
from typing import Any

from ossbot.common.schemas import Proposal
from ossbot.runner import prompts
from ossbot.runner.gk_client import GkError
from ossbot.runner.outputs import SCHEMA_HINTS
from ossbot.runner.pipeline import Deferred, StageContext, StageRun
from ossbot.runner.stages.common import issue_text, job, owner_notes

STAGE = "scout"
SKIP_CODES = {"above_autonomous_max": "too_big_for_auto", "kind_filtered": "kind_filtered",
              "never_repo": "never_repo", "above_ceiling": "above_ceiling"}  # fmt: skip


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


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid, repo, state = str(c["id"]), str(c["upstream"]), str(c["state"])
    if state in {"VETTED", "PROPOSAL_SNOOZED", "PROPOSAL_EXPIRED"}:
        sc.transition(cid, state, "SCOUTING")
        state = "SCOUTING"
    user = c.get("source") == "user"
    feedback = json.loads(c["proposal_feedback_json"]) if c.get("proposal_feedback_json") else None
    with StageRun(sc, cid, STAGE, profile="research") as run_:
        depth_args = {"repo": repo, "clone_depth": 1, "prefetch": False}
        job(sc, cid, STAGE, "fetch", "fetch", depth_args, "rw")
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
        if c.get("vetting_json"):
            sections.append(prompts.untrusted("vet facts", str(c["vetting_json"])))
        if feedback is not None:
            sections.append(prompts.human(f"Revise proposal v{feedback['version']}: {feedback['text']}"))
        text = prompts.task("scout", "Study the repository snapshot and propose one change for approval.",
                            sections, SCHEMA_HINTS["scout"])  # fmt: skip
        result = sc.kiro().invoke(cid, "scout", Proposal, text, timeout_s=1200, schema_hint=SCHEMA_HINTS["scout"])
        run_.model_actual = result.model_actual
        revises = int(c["proposal_version"]) if state != "SCOUTING" and c.get("proposal_version") else None
        try:
            sc.gk.submit_proposal(cid, result.value.model_dump(by_alias=True), revises,
                                  feedback["text"] if feedback else None)  # fmt: skip
        except GkError as exc:
            if exc.status == 429:
                raise Deferred("proposal_daily_cap") from exc
            if exc.status == 422 and exc.code in SKIP_CODES and state == "SCOUTING":
                skip = SKIP_CODES[exc.code]
            else:
                raise
        else:
            return
    sc.transition(cid, "SCOUTING", "SKIPPED", {"reason": skip})
    if user:
        sc.say(f"🛑 {repo}: the scouted change is {skip.replace('_', ' ')}; nothing was started.")
