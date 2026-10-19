"""The 🔀 Interdiff view of a revised card (DESIGN §14.11.7): the file-view message kind ``interdiff``.

``INTERDIFF`` mode shows the interdiff patch like a file view (same comment features); ``RANGE_DIFF``
mode shows the range-diff text in ``<pre>`` pages with no language (it is not a valid diff), and
replies to it are file-level only (the file whose ``## path ##`` section holds the quote).
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.review import explorer, patchview
from ossbot.gatekeeper.telegram.ui import kit as k

PAGE = 3000
RANGE_FILE = re.compile(r"^\s*## (.+?) ##\s*$")  # git range-diff's per-file section header


def path_for_quote(ctx: AppContext, cid: str, rev: int, quote: str) -> str | None:
    """The file whose range-diff section holds the first line of ``quote`` (file-level comments)."""
    wanted = next((q.strip() for q in quote.splitlines() if q.strip()), "")
    if not wanted:
        return None
    try:
        text = (ctx.gitops.derived_dir(cid, rev) / "interdiff.patch").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    current: str | None = None
    for line in text.splitlines():
        m = RANGE_FILE.match(line)
        if m:
            current = m.group(1)
        elif wanted in line:
            return current
    return None


def view(ctx: AppContext, cid: str, rev: int, action_id: str | None, page: int = 1) -> k.View:
    derived = ctx.gitops.derived_dir(cid, rev)
    try:
        info = json.loads((derived / "interdiff.json").read_text(encoding="utf-8"))
        text = (derived / "interdiff.patch").read_bytes().decode("utf-8", "replace")
    except (OSError, ValueError):
        info, text = {"mode": "unavailable"}, ""
    head = (k.Const("🔀 "), k.Bold(k.Const(f"{cid} r{rev - 1} → r{rev}")))
    if info.get("mode") == "interdiff":
        subject = explorer.subject(ctx.conn, derived, cid, rev, action_id)
        subject = replace(subject, files=patchview.parse(text))
        out = explorer.file_view(subject, 0, page)
        out.kind = "interdiff"
        out.blocks.insert(0, k.Block((k.Lines((head,)),), name="interdiff"))
        return out
    if info.get("mode") == "range_diff":
        chunks = [text[i : i + PAGE] for i in range(0, len(text), PAGE)] or ["(empty)"]
        page = min(max(1, page), len(chunks))
        note = k.line("base moved: line-level touch check unavailable; replies here are file-level comments")
        blocks = [k.Block((k.Lines((head, note)),)), k.Block((k.PreBlock(chunks[page - 1]),))]
        rows = [k.pager("nav", {"cid": cid, "rev": rev, "action_id": action_id, "view": "interdiff"}, page,
                        len(chunks))] if len(chunks) > 1 else []  # fmt: skip
        return k.View(kind="interdiff", blocks=blocks, rows=rows, cid=cid, rev=rev, action_id=action_id, page=page)
    note = k.line("Interdiff unavailable — review the full diff (🧾 Files)")
    return k.View(kind="interdiff", blocks=[k.Block((k.Lines((head, note)),))], cid=cid, rev=rev, action_id=action_id)
