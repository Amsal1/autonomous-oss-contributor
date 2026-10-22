"""Review documents (DESIGN §14.11.4): ``📄 .patch``, ``🌐 HTML`` and ``📄 pr_body.md``.

All three carry publish bytes, so they are ``scan()``ed and never altered (U14): a hit is a bug
(packaging already refused it) and sends nothing but a fixed note. Rendering the HTML is CPU work, so the
caller spawns this as a ``render`` task (§14.18.5).
"""

from __future__ import annotations

import asyncio
import logging

from ossbot.gatekeeper import db, review_html
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.review import patchview
from ossbot.gatekeeper.telegram.ui import ids
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import redact as ui_redact
from ossbot.gatekeeper.telegram.ui import send as ui_send

log = logging.getLogger(__name__)
WITHHELD = "⚠️ Document withheld: it matched a secret pattern (bug) · see /logs"


def _note(ctx: AppContext, text: str, thread_id: int | None) -> None:
    ui_send.deliver(ctx, k.plain("fyi", k.line(text)), thread_id=thread_id)


async def send(ctx: AppContext, which: str, cid: str, rev: int, thread_id: int | None) -> None:
    contrib = db.one(ctx.conn, "SELECT * FROM contributions WHERE id = ?", (cid,))
    if contrib is None:
        return
    derived = ctx.gitops.derived_dir(cid, rev)
    title = contrib["pr_title_override"] if contrib["pr_title_override"] is not None else contrib["pr_title"]
    body = contrib["pr_body_override"] if contrib["pr_body_override"] is not None else contrib["pr_body"]
    if which == "doc_body":
        data = (body or "").encode("utf-8")
        name, caption = "pr_body.md", f"📄 PR body · {cid} r{rev} · the bytes that will be published"
    elif which == "doc_patch":
        try:
            data = (derived / "change.patch").read_bytes()
        except OSError:
            _note(ctx, "Patch unavailable (derived artifacts gone).", thread_id)
            return
        name, caption = f"{cid}-r{rev}.patch", f"📄 {cid} r{rev} · gatekeeper-derived patch"
    else:
        files = patchview.load(derived)
        try:
            data = await asyncio.to_thread(
                review_html.render, cid=cid, rev=rev, upstream=str(contrib["upstream"]),
                facts=[f"{len(files)} files +{sum(f.added for f in files)} −{sum(f.deleted for f in files)}"],
                files=files, title=title or "", body=body or "", commit_message=contrib["commit_message"] or "",
            )  # fmt: skip
        except Exception:
            log.exception("HTML diff render failed", extra={"event": "review_html_failed"})
            _note(ctx, "HTML view unavailable", thread_id)
            return
        added, deleted = sum(f.added for f in files), sum(f.deleted for f in files)
        name = f"ossbot-review-{cid}-r{rev}.html"
        caption = f"🌐 Full diff · {len(files)} files +{added} −{deleted} · r{rev} · opens in your browser or Files app"
    if ui_redact.scan(data, surface=which):
        _note(ctx, WITHHELD, thread_id)
        return
    ui_send.document(ctx, name, data, caption=ids.autocode(caption), thread_id=thread_id)  # the cid one tap to copy
