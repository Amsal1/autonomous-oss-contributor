"""The final assistant answer as kit views (DESIGN §14.12.1 step 4, §14.12.3, §14.12.4, U6).

* The body is a ``kit.Markdown`` element (redacted by the renderer, converted by ``md2blocks``); flat
  mode and ✅ Reviews put it under the gatekeeper header ``🤖 Assistant · answer to your message`` in
  one quote (U6), so an answer can never look like a card.
* Long answers are split at block boundaries (≤ 28,000 characters rich, ≤ 2,400 escaped characters
  classic); more than three parts also get ``answer.md`` (built by the caller from the same text).
* Sources: a closed ``details`` block "Sources (3)" (classic: an expandable quote). Footer:
  ``🧠 <model> · 🔎 2 · 📄 3 · 14 s`` plus ``⚠️ fallback model``, ``👁 vision: <model>``, ``⏹ Stopped by
  you`` or ``…(truncated)``.
* Follow-ups: one ``chat_followup`` button per row (≤ 3), on the last part; the labels are also kept in
  the view state, so a tap is checked against what was rendered (§14.12.4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit

from ossbot.gatekeeper.chat import md2blocks
from ossbot.gatekeeper.telegram.escape import defuse_commands, esc, make_visible
from ossbot.gatekeeper.telegram.ui import blocks as rb
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui.limits import middle_cut
from ossbot.gatekeeper.telegram.ui.sanitize import domain_of

HEADER = "🤖 Assistant · answer to your message"
RICH_PART = 28_000
RICH_PART_BLOCKS = 400
CLASSIC_PART = 3300  # characters of HTML; the header, sources and footer fit in the rest of 3,900
DOCUMENT_PARTS = 3
FOLLOWUP_TTL = timedelta(hours=24)
EndKind = Literal["completed", "stopped", "truncated"]
Mode = Literal["rich", "rich_quoted", "classic", "quoted"]
_FENCE_OPEN = re.compile(r"^\s{0,3}(```+|~~~+)([^`]*)$")


@dataclass
class Answer:
    turn_id: int
    text: str  # redacted; a stopped or truncated stream already ends with "…"
    end: EndKind = "completed"
    sources: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    model: str = ""
    fallback: bool = False
    vision_model: str | None = None
    searches: int = 0
    fetches: int = 0
    elapsed_s: int = 0


# ------------------------------------------------------------------------------------ split
def _blocks(text: str) -> list[str]:
    """Markdown blocks separated by blank lines; a fence is one block however many blank lines it has."""
    out: list[str] = []
    cur: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        m = _FENCE_OPEN.match(line)
        if fence is None and m:
            fence = m.group(1)[:3]
        elif fence is not None and line.strip().startswith(fence):
            fence = None
            cur.append(line)
            continue
        if fence is None and not line.strip():
            if cur:
                out.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        out.append("\n".join(cur))
    return out


Cost = tuple[int, int]  # (characters, rich blocks); classic counts only characters


def _cost(block: str, mode: Mode) -> Cost:
    """What one Markdown block costs once rendered (additive: renderers join blocks independently)."""
    if mode == "rich":
        count, chars, _ = rb.measure(md2blocks.rich_blocks(block))
        return chars, count
    if mode == "rich_quoted":  # one paragraph per non-empty line inside the quote
        return len(block), sum(1 for ln in block.split("\n") if ln.strip())
    if mode == "quoted":
        return len(esc(defuse_commands(make_visible(block)[0]))) + 2, 0
    return len(md2blocks.classic_html(block)) + 2, 0


def _fits(cost: Cost, mode: Mode) -> bool:
    if mode in {"rich", "rich_quoted"}:
        return cost[0] <= RICH_PART and cost[1] <= RICH_PART_BLOCKS
    return cost[0] <= CLASSIC_PART


def _halves(block: str) -> list[str]:
    """Split an oversize block in two near its middle (a newline, else a space, else anywhere); a code
    fence stays a fence in both halves."""
    lines = block.split("\n")
    m = _FENCE_OPEN.match(lines[0]) if lines else None
    if m and len(lines) > 2:
        opener, closer = lines[0].strip(), m.group(1)[:3]
        inner = lines[1:-1] if lines[-1].strip().startswith(closer) else lines[1:]
        if len(inner) > 1:
            mid = len(inner) // 2
            return [f"{opener}\n" + "\n".join(half) + f"\n{closer}" for half in (inner[:mid], inner[mid:])]
        text = inner[0] if inner else ""
        mid = len(text) // 2
        return [f"{opener}\n{text[:mid]}\n{closer}", f"{opener}\n{text[mid:]}\n{closer}"]
    mid = len(block) // 2
    cut_at = block.rfind("\n", 0, mid)
    if cut_at <= 0:
        cut_at = block.rfind(" ", 0, mid)
    if cut_at <= 0:
        cut_at = mid
    return [block[:cut_at], block[cut_at:].lstrip("\n ")]


def _pieces(block: str, mode: Mode) -> list[tuple[str, Cost]]:
    cost = _cost(block, mode)
    if _fits(cost, mode) or len(block) <= 1:
        return [(block, cost)]
    out: list[tuple[str, Cost]] = []
    for half in _halves(block):
        if half:
            out += _pieces(half, mode)
    return out


def split(text: str, *, rich: bool, quoted: bool = False) -> list[str]:
    """Parts at block boundaries, each within one message's budget once rendered (rich ≤ 28,000
    characters and 400 blocks; classic ≤ 3,300 characters of HTML)."""
    mode: Mode = ("rich_quoted" if quoted else "rich") if rich else ("quoted" if quoted else "classic")
    parts: list[str] = []
    cur, cur_cost = "", (0, 0)
    for block in _blocks(text):
        for piece, cost in _pieces(block, mode):
            total = (cur_cost[0] + cost[0], cur_cost[1] + cost[1])
            if cur and not _fits(total, mode):
                parts.append(cur)
                cur, total = "", cost
            cur = f"{cur}\n\n{piece}" if cur else piece
            cur_cost = total
    if cur:
        parts.append(cur)
    return parts or [""]


# ------------------------------------------------------------------------------------ pieces
def footer(a: Answer) -> k.Line:
    bits = [f"🧠 {a.model or 'assistant'}"]
    if a.vision_model:
        bits.append(f"👁 vision: {a.vision_model}")
    if a.searches:
        bits.append(f"🔎 {a.searches}")
    if a.fetches:
        bits.append(f"📄 {a.fetches}")
    bits.append(f"{a.elapsed_s} s")
    if a.fallback:
        bits.append("⚠️ fallback model")
    if a.end == "stopped":
        bits.append("⏹ Stopped by you")
    elif a.end == "truncated":
        bits.append("…(truncated)")
    return (k.Text(" · ".join(bits), 300),)


def _source_label(url: str) -> str:
    host = domain_of(url) or url
    path = urlsplit(url).path.strip("/")
    return f"{host} — {middle_cut(path, 60)}" if path else host


def sources(urls: list[str]) -> k.DetailsEl:
    lines = tuple((k.Link(k.Text(_source_label(u), 120), u),) for u in urls)
    return k.DetailsEl((k.Const(f"Sources ({len(urls)})"),), lines)


def followup_rows(a: Answer) -> k.Rows:
    return [[k.button(label, "chat_followup", {"text": label, "turn_id": a.turn_id}, single_use=True,
                      ttl=FOLLOWUP_TTL)] for label in a.followups]  # fmt: skip


def views(a: Answer, *, wrapped: bool, rich: bool, extra: k.Line | None = None, rows: k.Rows | None = None,
          header: str = HEADER) -> list[k.View]:  # fmt: skip
    """The answer's messages. ``wrapped``: header + one quote (flat mode, Reviews); ``rich``: the view
    may be sent rich (never in Reviews); ``extra``: one gatekeeper line under the header (stale or
    origin note); ``rows``: extra buttons on the last part (e.g. ``Use as comment``)."""
    texts = split(a.text, rich=rich, quoted=wrapped)
    out: list[k.View] = []
    for i, part in enumerate(texts):
        last = i == len(texts) - 1
        head: list[k.Line] = []
        if wrapped and i == 0:
            head.append((k.Const(header),))
        if extra is not None and i == 0:
            head.append(extra)
        blocks: list[k.Block] = []
        if head:
            blocks.append(k.Block((k.Lines(tuple(head)),), name="head"))
        blocks.append(k.Block((k.Markdown(part or "…", quoted=wrapped),), name="answer"))
        view_rows: k.Rows = []
        state: dict[str, object] = {"turn_id": a.turn_id, "part": i}
        preview = None
        if last:
            if a.sources:
                blocks.append(k.Block((sources(a.sources),), required=False, priority=2, name="sources"))
                if len(a.sources) == 1 and not rich:
                    preview = a.sources[0]
            blocks.append(k.Block((k.FooterEl(footer(a)),), required=False, priority=1, name="footer"))
            view_rows = followup_rows(a) + list(rows or [])
            state["followups"] = list(a.followups)
        out.append(k.View(kind="chat_answer", blocks=blocks, rows=view_rows, state=state, rich=rich,
                          link_preview=preview))  # fmt: skip
    return out


def needs_document(a: Answer, *, rich: bool, wrapped: bool) -> bool:
    return len(split(a.text, rich=rich, quoted=wrapped)) > DOCUMENT_PARTS
