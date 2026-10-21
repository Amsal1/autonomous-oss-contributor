"""Fetch (DESIGN §9.6): the build clone + dependency prefetch in the task volume (profile ``fetch``).

PROPOSAL_APPROVED -> FETCHING only after Start work (the gatekeeper guards the edge with the consumed
approval of the current version and, behind gate 1, the claim). When a claim is required and none
exists yet, the runner requests the ``issue_claim`` action (its own approval card; dry-run applies)
and waits. Operator imports enter from IMPORTED with their fixed base.
"""

from __future__ import annotations

from typing import Any

from ossbot.runner.gk_client import GkError
from ossbot.runner.pipeline import Deferred, StageContext, StageRun
from ossbot.runner.stages.common import artifact_json, job, target

STAGE = "fetch"
CLAIM_BODY = (
    "Hi! I'd like to work on this issue and open a pull request with a fix and tests. "
    "Please let me know if someone else is already on it or if you'd prefer a different approach."
    "\n\n— drafted with AI assistance, reviewed by @{login}"
)


def _claim(sc: StageContext, c: dict[str, Any]) -> None:
    cid = str(c["id"])  # the gatekeeper's guard applies only to items with an issue
    if sc.load(cid).get("claim_requested"):
        raise Deferred("waiting_for_claim")
    login = sc.config().profile.github_login
    sc.gk.action(cid, "issue_claim", int(c["issue_number"]), CLAIM_BODY.format(login=login))
    sc.save(cid, claim_requested=True)
    raise Deferred("claim_requested")


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid, repo, state = str(c["id"]), str(c["upstream"]), str(c["state"])
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
