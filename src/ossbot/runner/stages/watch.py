"""Watch (DESIGN §9.12): PR state, checks, reviews and new maintainer feedback every 15 min.

Merged/closed -> MERGED/CLOSED. New feedback -> a drafted reply (a ``comment`` action behind its own
approval; WATCHING -> WATCHING, the gatekeeper counts ``watch_rounds``) or a fix round
(WATCHING -> IMPLEMENTING, one new commit, ``push_update`` behind approval). Rebase/squash requests
go to the owner. After ``watch_rounds_max`` the runner only notifies.

Feedback is read from three sources (owner decision 2026-10-23 (b)): issue comments (``since``), pull
reviews (no ``since`` on GitHub: kept when ``submitted_at`` is after the watermark and the review has a
body or requests changes) and inline review comments (``since``). The owner's own items and every bot
item (``common.github_bots.is_bot``: ``user.type == "Bot"`` or a ``[bot]`` login, classified in code)
are dropped before the watch decision: a bot never reaches the model, never drafts a reply and never
starts a fix (review-bot findings are the gatekeeper's finding cards instead). The watermark is stamped
before the first read, so an item posted during the reads is read again next time, never lost.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ossbot.common.github_bots import is_bot, is_owner
from ossbot.common.project import comment_disclosure
from ossbot.runner import prompts
from ossbot.runner.gk_client import GkError
from ossbot.runner.outputs import WatchDecision
from ossbot.runner.pipeline import StageContext, StageRun

STAGE = "watch"
INTERVAL_S = 900
ITEMS_SHOWN = 20
SCHEMA = '{"action": "reply|fix|rebase|none", "reply_body": "...", "fix_request": "..."}'


def task_text(repo: str, number: int, body: str) -> str:
    """The watch decision task: the summarizer agent with the watcher's role prompt and schema (no new
    model role); the PR comments are untrusted data."""
    return prompts.task(
        "summarizer",
        "Decide what the new PR feedback needs: reply, fix, rebase or none.",
        [prompts.untrusted(f"github:{repo}#{number} new comments", body)],
        SCHEMA,
        role_file="watcher_reply",
        schema_name="watcher",
    )


def now_stamp() -> str:
    """The watermark of this poll (UTC, GitHub's ``since`` format); taken before the reads."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def due(sc: StageContext, c: dict[str, Any]) -> bool:
    last = sc.load(str(c["id"])).get("watched_at")
    return last is None or sc.clocks.wall() - float(last) >= INTERVAL_S


def _login(item: dict[str, Any]) -> str:
    return str((item.get("user") or {}).get("login") or "?")


def read_feedback(sc: StageContext, repo: str, number: int, since: str | None) -> tuple[list[tuple[str, str]], bool]:
    """(the human, non-owner items as ``(at, text)`` in time order, whether any read returned an item).
    Bots count for the watermark but never for the decision."""
    owner = sc.config().profile.github_login
    raw: list[tuple[str, str, dict[str, Any]]] = []  # (at, header, item)
    for cm in sc.gh.comments(repo, number, since):
        raw.append((str(cm.get("created_at") or ""), f"## {_login(cm)} (comment)", cm))
    for rv in sc.gh.pull_reviews(repo, number):
        at = str(rv.get("submitted_at") or "")
        if since and at <= since:
            continue
        state = str(rv.get("state") or "")
        if not str(rv.get("body") or "").strip() and state != "CHANGES_REQUESTED":
            continue
        raw.append((at, f"## {_login(rv)} (review: {state or '?'})", rv))
    for ic in sc.gh.pull_review_comments(repo, number, since):
        line = ic.get("line") or ic.get("original_line")
        where = f"{str(ic.get('path') or '?')[:200]}:{line if isinstance(line, int) else '?'}"
        raw.append((str(ic.get("created_at") or ""), f"## {_login(ic)} (inline on {where})", ic))
    items = [(at, f"{head}\n{item.get('body') or ''}") for at, head, item in raw
             if not is_bot(item.get("user")) and not is_owner(item.get("user"), owner)]  # fmt: skip
    items.sort(key=lambda it: it[0])
    return items, bool(raw)


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid, repo, number = str(c["id"]), str(c["upstream"]), c.get("pr_number")
    if number is None:
        return
    notes = sc.load(cid)
    since = notes.get("watermark")
    stamp = now_stamp()  # before the reads: an item posted meanwhile is read again next poll
    with StageRun(sc, cid, STAGE, profile="research") as run_:
        pr = sc.gh.pull(repo, int(number)) or {}
        new, seen = read_feedback(sc, repo, int(number), since)
        decision = WatchDecision(action="none")
        if new and pr.get("state") == "open":
            limits = sc.config().policy.limits
            if int(c.get("watch_rounds") or 0) >= limits.watch_rounds_max:
                sc.say(f"👀 PR #{number} on {repo} needs your attention ({len(new)} new comments).")
            else:
                body = "\n\n".join(text for _, text in new[-ITEMS_SHOWN:])
                text = task_text(repo, int(number), body)
                result = sc.kiro().invoke(cid, "summarizer", WatchDecision, text, timeout_s=600, schema_hint=SCHEMA)
                run_.model_actual = result.model_actual
                decision = result.value
    sc.save(cid, watched_at=sc.clocks.wall(), watermark=stamp if seen else since)
    if pr.get("merged_at"):
        sc.transition(cid, "WATCHING", "MERGED", {"reason": "merged"})
        return
    if pr.get("state") == "closed":
        sc.transition(cid, "WATCHING", "CLOSED", {"reason": "closed_unmerged"})
        return
    cfg = sc.config()
    if decision.action == "reply" and decision.reply_body.strip():
        footer = comment_disclosure(cfg.profile.github_login, cfg.policy.text.project_url)
        reply = f"{decision.reply_body.strip()}\n\n{footer}"
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
