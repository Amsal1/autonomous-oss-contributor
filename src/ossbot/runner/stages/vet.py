"""Vet (DESIGN §9.5): deterministic repo/issue facts, the vetter, the conservative merge rule.

DISCOVERED -> VETTING -> VETTED (evidence ``vetting`` kept on the row) | SKIPPED <reason>.
AI ban: SKIPPED for every item (👤 too: the owner is told why). ``unclear``: SKIPPED for autonomous
items unless the target allows AI. CLA without ``cla_signed``: SKIPPED for autonomous items; 👤
items continue and the Start-work confirm says so. ``requires_assignment`` comes from CONTRIBUTING
(``ASSIGNMENT_RE``) or the vetter; the claim is created only after gate 1 (§9.2).
"""

from __future__ import annotations

import json
import re
from typing import Any

from ossbot.runner import prompts
from ossbot.runner.outputs import SCHEMA_HINTS, IssueVetting
from ossbot.runner.pipeline import StageContext, StageRun
from ossbot.runner.stages.common import issue_text, owner_notes, target

STAGE = "vet"
CONTRIBUTING_PATHS = ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md")
AI_POLICY_PATHS = ("AI_POLICY.md", ".github/AI_POLICY.md")
AI_SECTION_RE = re.compile(r"\b(AI|LLM|generative|Copilot|ChatGPT|Claude|machine[- ]generated)\b", re.IGNORECASE)
BAN_RE = re.compile(
    r"(no ai\b|ai[- ]generated (code|contributions|pull requests|prs) (are|is) not (accepted|allowed|welcome)"
    r"|do not (submit|use) (llm|ai)|(llm|ai)[- ](generated|assisted) .{0,40}(prohibited|banned|forbidden))",
    re.IGNORECASE,
)
ASSIGNMENT_RE = re.compile(
    r"(must be assigned|get (yourself )?assigned|ask to be assigned|wait (to be|until you are) assigned"
    r"|comment .{0,40}before (opening|submitting|starting)|claim (the|an) issue)",
    re.IGNORECASE,
)
SIGNED_RE = re.compile(r"\b(signed commits|gpg[- ]sign|commit signing) (is |are )?(required|mandatory)", re.I)


def _facts(sc: StageContext, repo: str) -> dict[str, Any]:
    info = sc.gh.repo(repo) or {}
    contributing_path, contributing = next(((p, t) for p in CONTRIBUTING_PATHS if (t := sc.gh.file_text(repo, p))),
                                           ("", ""))  # fmt: skip
    policy_path, ai_policy = next(((p, t) for p in AI_POLICY_PATHS if (t := sc.gh.file_text(repo, p))), ("", ""))
    ai_sections = "\n".join(line for line in contributing.splitlines() if AI_SECTION_RE.search(line))
    # the line that bans AI help, for the gatekeeper's refusal card of a 👤 request (§14.11.19)
    ban_line = next((ln for ln in ai_policy.splitlines() if BAN_RE.search(ln)), "")
    ban_path = policy_path if ban_line else ""
    if not ban_line:
        ban_line = next((ln for ln in ai_sections.splitlines() if BAN_RE.search(ln)), "")
        ban_path = contributing_path if ban_line else ""
    return {
        "archived": bool(info.get("archived")), "default_branch": info.get("default_branch"),
        "language": info.get("language"), "stars": info.get("stargazers_count"),
        "pushed_at": info.get("pushed_at"), "contributing": contributing, "ai_policy": ai_policy,
        "ai_sections": ai_sections,
        "ban_phrase": bool(BAN_RE.search(ai_policy) or BAN_RE.search(ai_sections)),
        "assignment_required": bool(ASSIGNMENT_RE.search(contributing)),
        "signed_commits": bool(SIGNED_RE.search(contributing)),
        "ban_line": ban_line.strip()[:300], "ban_path": ban_path,
    }  # fmt: skip


def merge(vetting: IssueVetting, facts: dict[str, Any], *, user: bool, ai_allowed: bool,
          cla_signed: bool) -> tuple[str | None, dict[str, Any]]:  # fmt: skip
    """The deterministic, conservative merge (§9.5). Returns (skip reason or None, stored vetting)."""
    policy = "banned" if facts["ban_phrase"] or vetting.ai_policy == "banned" else vetting.ai_policy
    stored = {
        **vetting.model_dump(), "ai_policy": policy,
        "requires_assignment": bool(vetting.requires_assignment or facts["assignment_required"]),
        "requires_claim_comment": bool(vetting.requires_claim_comment or vetting.claim_status == "claim_required"),
        "signed_commits_required": bool(facts["signed_commits"]), "user_directed": user,
        "cla_signed": cla_signed,
    }  # fmt: skip
    if facts["archived"]:
        return "archived", stored
    if policy == "banned":
        return "ai_policy_banned", stored
    if policy == "unclear" and not ai_allowed and not user:
        return "ai_policy_unclear", stored
    if vetting.claim_status == "claimed_by_other":
        return "claimed_by_other", stored
    if stored["signed_commits_required"]:
        return "signed_commits_required", stored
    if vetting.cla_required and not cla_signed and not user:
        return "cla_required", stored
    return None, stored


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid, repo = str(c["id"]), str(c["upstream"])
    if c["state"] == "DISCOVERED":
        sc.transition(cid, "DISCOVERED", "VETTING")
    tgt = target(sc, repo)
    user = c.get("source") == "user"
    with StageRun(sc, cid, STAGE, profile="research") as run_:
        facts = _facts(sc, repo)
        issue, comments = issue_text(sc, c)
        private = {"contributing", "ai_policy", "ban_line", "ban_path"}  # untrusted text goes in the sections
        files = {"facts.json": json.dumps({k: v for k, v in facts.items() if k not in private})}
        sections = [issue, comments, *owner_notes(c),
                    prompts.untrusted(f"github:{repo} CONTRIBUTING", facts["contributing"] or "(none)"),
                    prompts.untrusted(f"github:{repo} AI policy", facts["ai_policy"] or "(none)")]  # fmt: skip
        text = prompts.task("vetter", "Vet this issue for an AI-assisted outside contribution.", sections,
                            SCHEMA_HINTS["vetter"])  # fmt: skip
        result = sc.kiro().invoke(cid, "vetter", IssueVetting, text, files, timeout_s=900,
                                  schema_hint=SCHEMA_HINTS["vetter"])  # fmt: skip
        run_.model_actual = result.model_actual
        reason, stored = merge(result.value, facts, user=user, ai_allowed=tgt.ai_policy_override == "allowed",
                               cla_signed=tgt.cla_signed)  # fmt: skip
    if reason is not None:
        evidence: dict[str, Any] = {"reason": reason}
        if reason == "ai_policy_banned":  # quoted on the gatekeeper's refusal card (§14.11.19)
            line = facts["ban_line"] or next((e.quote for e in result.value.ai_policy_evidence), "")
            evidence.update({"policy_line": line.strip()[:300], "policy_path": facts["ban_path"]})
        sc.transition(cid, "VETTING", "SKIPPED", evidence)
        noted = sc.load("_repos").get("cla_noted", [])
        if reason == "cla_required" and repo not in noted:  # one note per repo (§9.5)
            sc.save("_repos", cla_noted=[*noted, repo])
            sc.say(f"✍️ {repo} requires a CLA: sign it and set cla_signed: true in targets.yaml to unlock it.")
        if user and reason != "ai_policy_banned":  # that one gets the gatekeeper's refusal card
            sc.say(f"🛑 Not working on {repo} for your request: {reason.replace('_', ' ')}.")
        return
    sc.transition(cid, "VETTING", "VETTED", {"vetting": stored})
