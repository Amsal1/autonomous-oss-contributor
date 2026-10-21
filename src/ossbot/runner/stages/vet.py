"""Vet (DESIGN §9.5): deterministic repo/issue facts, the vetter, the conservative merge rule.

DISCOVERED -> VETTING -> VETTED (evidence ``vetting`` kept on the row) | SKIPPED <reason>.
AI policy (post-v1 owner rule, ``policy.vet.ai_policy``): only an explicit ban skips, for every item
(👤 too: the owner gets the refusal card). The ban comes from the deterministic phrase rules over the
repo docs and maintainer comments (``common.ai_policy``) or from the vetter; the vetter can't override
a deterministic ban, and its allow claim counts only with a verbatim quote from a fetched repo doc.
``unstated``/``unclear`` are allowed unless the owner sets ``skip`` (per policy or per target); the
``Allow AI`` override and 👤 bypass that rule. Conditions of an allow-with-conditions policy are
recorded (``ai_conditions``) and applied by the package stage. CLA without ``cla_signed``: SKIPPED for
autonomous items; 👤 items continue and the Start-work confirm says so. ``requires_assignment`` comes
from CONTRIBUTING (``ASSIGNMENT_RE``) or the vetter; the claim is created only after gate 1 (§9.2).
"""

from __future__ import annotations

import json
import re
from typing import Any

from ossbot.common import ai_policy
from ossbot.common.ai_policy import AI_SECTION_RE, Classification
from ossbot.common.config import AiPolicyRules, effective_ai_rules
from ossbot.runner import prompts
from ossbot.runner.outputs import SCHEMA_HINTS, IssueVetting
from ossbot.runner.pipeline import StageContext, StageRun
from ossbot.runner.repo_docs import fetch_policy_docs, maintainer_docs
from ossbot.runner.stages.common import fetch_issue, owner_notes, render_issue, target

STAGE = "vet"
ASSIGNMENT_RE = re.compile(
    r"(must be assigned|get (yourself )?assigned|ask to be assigned|wait (to be|until you are) assigned"
    r"|comment .{0,40}before (opening|submitting|starting)|claim (the|an) issue)",
    re.IGNORECASE,
)
SIGNED_RE = re.compile(r"\b(signed commits|gpg[- ]sign|commit signing) (is |are )?(required|mandatory)", re.I)
FULL_TEXT_GROUPS = ("contributing", "ai_policy")  # quoted in full; the other docs only by their AI lines
PRIVATE_FACTS = {"contributing", "ai_policy_text", "docs", "classification"}  # not in facts.json


def _facts(sc: StageContext, repo: str, maintainer: list[ai_policy.PolicyDoc]) -> dict[str, Any]:
    info = sc.gh.repo(repo) or {}
    docs = fetch_policy_docs(sc.gh, repo)
    contributing = docs["contributing"].text if "contributing" in docs else ""
    classification = ai_policy.classify([*docs.values(), *maintainer])
    return {
        "archived": bool(info.get("archived")), "default_branch": info.get("default_branch"),
        "language": info.get("language"), "stars": info.get("stargazers_count"),
        "pushed_at": info.get("pushed_at"), "contributing": contributing,
        "ai_policy_text": docs["ai_policy"].text if "ai_policy" in docs else "",
        "policy_docs": sorted(d.path for d in docs.values()), "docs": docs,
        "ai_policy_status": classification.status, "classification": classification,
        "assignment_required": bool(ASSIGNMENT_RE.search(contributing)),
        "signed_commits": bool(SIGNED_RE.search(contributing)),
    }  # fmt: skip


def merge(vetting: IssueVetting, facts: dict[str, Any], *, user: bool, ai_allowed: bool, cla_signed: bool,
          rules: AiPolicyRules | None = None) -> tuple[str | None, dict[str, Any]]:  # fmt: skip
    """The deterministic, conservative merge (§9.5). Returns (skip reason or None, stored vetting)."""
    rules = rules or AiPolicyRules()
    det: Classification = facts["classification"]
    doc_texts = [d.text for d in facts.get("docs", {}).values()]
    merged = ai_policy.merge_claim(det, vetting.ai_policy, [e.quote for e in vetting.ai_policy_evidence],
                                   list(vetting.ai_conditions), doc_texts)  # fmt: skip
    policy = merged.status
    stored = {
        **vetting.model_dump(), "ai_policy": policy,
        "ai_conditions": [c.model_dump() for c in merged.conditions],
        "ai_policy_source": merged.source[:200],
        "ai_policy_line": (merged.ban_line if policy == "banned" else merged.allow_line)[:300],
        "requires_assignment": bool(vetting.requires_assignment or facts["assignment_required"]),
        "requires_claim_comment": bool(vetting.requires_claim_comment or vetting.claim_status == "claim_required"),
        "signed_commits_required": bool(facts["signed_commits"]), "user_directed": user,
        "cla_signed": cla_signed,
    }  # fmt: skip
    if facts["archived"]:
        return "archived", stored
    if policy == "banned":
        return "ai_policy_banned", stored
    if policy in {"unstated", "unclear"} and getattr(rules, policy) == "skip" and not ai_allowed and not user:
        return f"ai_policy_{policy}", stored
    if vetting.claim_status == "claimed_by_other":
        return "claimed_by_other", stored
    if stored["signed_commits_required"]:
        return "signed_commits_required", stored
    if vetting.cla_required and not cla_signed and not user:
        return "cla_required", stored
    return None, stored


def _doc_sections(repo: str, docs: dict[str, ai_policy.PolicyDoc]) -> list[str]:
    out = [
        prompts.untrusted(f"github:{repo} CONTRIBUTING", docs["contributing"].text if "contributing" in docs
                          else "(none)"),
        prompts.untrusted(f"github:{repo} AI policy", docs["ai_policy"].text if "ai_policy" in docs else "(none)"),
    ]  # fmt: skip
    for group, doc in docs.items():
        if group in FULL_TEXT_GROUPS:
            continue
        lines = [ln for ln in doc.text.splitlines() if AI_SECTION_RE.search(ln)][:40]
        if lines:
            out.append(prompts.untrusted(f"github:{repo} {doc.path} (AI-related lines)", "\n".join(lines)))
    return out


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid, repo = str(c["id"]), str(c["upstream"])
    if c["state"] == "DISCOVERED":
        sc.transition(cid, "DISCOVERED", "VETTING")
    tgt = target(sc, repo)
    user = c.get("source") == "user"
    with StageRun(sc, cid, STAGE, profile="research") as run_:
        raw_issue, raw_comments, number = fetch_issue(sc, c)
        facts = _facts(sc, repo, maintainer_docs(repo, number, raw_issue, raw_comments))
        issue, comments = render_issue(repo, number, raw_issue, raw_comments)
        files = {"facts.json": json.dumps({k: v for k, v in facts.items() if k not in PRIVATE_FACTS})}
        sections = [issue, comments, *owner_notes(c), *_doc_sections(repo, facts["docs"])]
        text = prompts.task("vetter", "Vet this issue for an AI-assisted outside contribution.", sections,
                            SCHEMA_HINTS["vetter"])  # fmt: skip
        result = sc.kiro().invoke(cid, "vetter", IssueVetting, text, files, timeout_s=900,
                                  schema_hint=SCHEMA_HINTS["vetter"])  # fmt: skip
        run_.model_actual = result.model_actual
        reason, stored = merge(result.value, facts, user=user, ai_allowed=tgt.ai_policy_override == "allowed",
                               cla_signed=tgt.cla_signed,
                               rules=effective_ai_rules(sc.config().policy, tgt))  # fmt: skip
    if reason is not None:
        evidence: dict[str, Any] = {"reason": reason}
        if reason == "ai_policy_banned":  # quoted on the gatekeeper's refusal card (§14.11.19)
            det: Classification = facts["classification"]
            line = det.ban_line or str(stored.get("ai_policy_line") or "")
            evidence.update({"policy_line": line.strip()[:300], "policy_path": det.ban_path})
        sc.transition(cid, "VETTING", "SKIPPED", evidence)
        noted = sc.load("_repos").get("cla_noted", [])
        if reason == "cla_required" and repo not in noted:  # one note per repo (§9.5)
            sc.save("_repos", cla_noted=[*noted, repo])
            sc.say(f"✍️ {repo} requires a CLA: sign it and set cla_signed: true in targets.yaml to unlock it.")
        if user and reason != "ai_policy_banned":  # that one gets the gatekeeper's refusal card
            sc.say(f"🛑 Not working on {repo} for your request: {reason.replace('_', ' ')}.")
        return
    sc.transition(cid, "VETTING", "VETTED", {"vetting": stored})
