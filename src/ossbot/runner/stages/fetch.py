"""Fetch (DESIGN §9.6): the build clone + dependency prefetch in the task volume (profile ``fetch``).

PROPOSAL_APPROVED -> FETCHING only after Start work (the gatekeeper guards the edge with the consumed
approval of the current version and, behind gate 1, the claim). When a claim is required and none
exists yet, the runner requests the ``issue_claim`` action (its own approval card; dry-run applies)
and waits. Operator imports enter from IMPORTED with their fixed base. After the human rerun-with-network
edge (ADR-0024) the failed run's notes are cleared once (``rerun_reset``) and the fetch starts from a fresh
clone as always.
"""

from __future__ import annotations

from typing import Any

from ossbot.common.project import comment_disclosure
from ossbot.runner.gk_client import GkError
from ossbot.runner.pipeline import Deferred, StageContext, StageRun
from ossbot.runner.stages.common import artifact_json, job, target

STAGE = "fetch"
CLAIM_BODY = (
    "Hi! I'd like to work on this issue and open a pull request with a fix and tests. "
    "Please let me know if someone else is already on it or if you'd prefer a different approach."
)


def _claim(sc: StageContext, c: dict[str, Any]) -> None:
    cid = str(c["id"])  # the gatekeeper's guard applies only to items with an issue
    if sc.load(cid).get("claim_requested"):
        raise Deferred("waiting_for_claim")
    cfg = sc.config()
    footer = comment_disclosure(cfg.profile.github_login, cfg.policy.text.project_url)
    sc.gk.action(cid, "issue_claim", int(c["issue_number"]), f"{CLAIM_BODY}\n\n{footer}")
    sc.save(cid, claim_requested=True)
    raise Deferred("claim_requested")


def rerun_reset(sc: StageContext, c: dict[str, Any]) -> bool:
    """ADR-0024: after the human rerun-with-network edge (``network_reruns`` moved past the note ``rerun_seen``),
    the failed run's notes go once: the validation comparison, the sandbox gap and the implement report. The claim
    note stays (the existing claim action is reused). True when it reset."""
    cid = str(c["id"])
    reruns = int(c.get("network_reruns") or 0)
    if reruns == int(sc.load(cid).get("rerun_seen") or 0):
        return False
    sc.save(cid, rerun_seen=reruns, validation_sig=None, validation_prev_sig=None, sandbox_gap=False,
            sandbox_gap_text=None, implement=None)  # fmt: skip
    return True


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid, repo, state = str(c["id"]), str(c["upstream"]), str(c["state"])
    if state == "PROPOSAL_APPROVED":
        rerun_reset(sc, c)
    if state in {"PROPOSAL_APPROVED", "IMPORTED"}:
        try:
            sc.transition(cid, state, "FETCHING")
        except GkError as exc:
            if exc.code == "guard_failed:claim":
                _claim(sc, c)
            raise
    tgt = target(sc, repo)
    args: dict[str, Any] = {"repo": repo, "clone_depth": max(2, tgt.clone_depth)}
    if c.get("source") == "operator":
        args.update(base_ref=c["base_ref"], base_sha=c["base_sha"])
    with StageRun(sc, cid, STAGE, profile="fetch"):
        done = job(sc, cid, STAGE, "fetch", "fetch", args, "rw", egress=tgt.egress)
        info = artifact_json(done, "fetch.json")
    sc.save(cid, fetch=info, fetch_artifacts=done.get("artifact_dir"))
    sc.transition(cid, "FETCHING", "FETCHED", {"base_sha": info.get("base_sha"), "base_ref": info.get("base_ref"),
                                               "ecosystems": info.get("ecosystems", [])})  # fmt: skip
