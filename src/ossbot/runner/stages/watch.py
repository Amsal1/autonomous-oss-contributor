"""Watch (DESIGN §9.12): PR state, checks, reviews and new maintainer feedback every 15 min.

Merged/closed -> MERGED/CLOSED. New feedback -> a drafted reply (a ``comment`` action behind its own
approval; WATCHING -> WATCHING, the gatekeeper counts ``watch_rounds``) or a fix round
(WATCHING -> IMPLEMENTING, one new commit, ``push_update`` behind approval). Rebase/squash requests
go to the owner. After ``watch_rounds_max`` the runner only notifies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ossbot.runner import prompts
from ossbot.runner.gk_client import GkError
from ossbot.runner.outputs import WatchDecision
from ossbot.runner.pipeline import StageContext, StageRun

STAGE = "watch"
INTERVAL_S = 900
SCHEMA = '{"action": "reply|fix|rebase|none", "reply_body": "...", "fix_request": "..."}'


def due(sc: StageContext, c: dict[str, Any]) -> bool:
    last = sc.load(str(c["id"])).get("watched_at")
    return last is None or sc.clocks.wall() - float(last) >= INTERVAL_S


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid, repo, number = str(c["id"]), str(c["upstream"]), c.get("pr_number")
    if number is None:
        return
    notes = sc.load(cid)
    since = notes.get("watermark")
    with StageRun(sc, cid, STAGE, profile="research") as run_:
        pr = sc.gh.pull(repo, int(number)) or {}
        new = [cm for cm in sc.gh.comments(repo, int(number), since)
               if (cm.get("user") or {}).get("login") != sc.config().profile.github_login]  # fmt: skip
        decision = WatchDecision(action="none")
        if new and pr.get("state") == "open":
            limits = sc.config().policy.limits
            if int(c.get("watch_rounds") or 0) >= limits.watch_rounds_max:
                sc.say(f"👀 PR #{number} on {repo} needs your attention ({len(new)} new comments).")
            else:
                body = "\n\n".join(f"## {(cm.get('user') or {}).get('login', '?')}\n{cm.get('body') or ''}"
                                   for cm in new[-20:])  # fmt: skip
                text = prompts.task(
                    "summarizer",
                    (prompts.prompts_dir() / "watcher_reply.md").read_text("utf-8"),
                    [prompts.untrusted(f"github:{repo}#{number} new comments", body)],
                    SCHEMA,
                )
                result = sc.kiro().invoke(cid, "summarizer", WatchDecision, text, timeout_s=600, schema_hint=SCHEMA)
                run_.model_actual = result.model_actual
                decision = result.value
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sc.save(cid, watched_at=sc.clocks.wall(), watermark=stamp if new else since)
    if pr.get("merged_at"):
        sc.transition(cid, "WATCHING", "MERGED", {"reason": "merged"})
        return
    if pr.get("state") == "closed":
        sc.transition(cid, "WATCHING", "CLOSED", {"reason": "closed_unmerged"})
        return
    login = sc.config().profile.github_login
    if decision.action == "reply" and decision.reply_body.strip():
        reply = decision.reply_body.strip() + f"\n\n— drafted with AI assistance, reviewed by @{login}"
        try:
            sc.gk.action(cid, "comment", int(number), reply)
            sc.transition(cid, "WATCHING", "WATCHING", {"reason": "reply_drafted"})
        except GkError as exc:
            sc.say(f"👀 PR #{number}: reply not queued ({exc.code}).")
    elif decision.action == "fix" and decision.fix_request.strip():
        sc.save(cid, watch_fix=decision.fix_request.strip())
        sc.transition(cid, "WATCHING", "IMPLEMENTING", {"reason": "fix_round"})
    elif decision.action == "rebase":
        sc.say(f"🔀 Maintainer asks for a rebase on PR #{number} ({repo}); handle it manually.")
