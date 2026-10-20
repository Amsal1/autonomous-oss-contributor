"""Opt-in Telegraph articles for long assistant answers (DESIGN §14.12.8; U9, U11; AC 27, 65, 68).

Off by default: the ``📖 Open as article`` button exists only while ``telegram.yaml telegraph.enabled``
is true (a human-confirmed ``/reload``, which also renders the gatekeeper-only ``telegraph`` egress) and
a ``TELEGRAPH_ACCESS_TOKEN`` exists (``ossctl telegraph setup``). Only ``chat_answer`` views can carry it,
and only for an eligible turn. ``eligible(turn_row)`` reads the **current** ``chat_turns`` row and is
re-evaluated when the button is rendered, at the tap, at Confirm and right before ``createPage``.

Publishing takes two taps and is refused while the system is paused or killed (at the tap, at Confirm
and right before the API call). The content is rebuilt from the stored, already redacted answer, then
scanned (secret patterns, the gatekeeper's own secret values, the Kiro key hash, ``gitleaks stdin``, the
confidentiality floor and denylist, the profile's email and name) with no override, converted to
Telegraph nodes with an allowlisted tag set (no media), and published under an unguessable title suffix.
The user's question is never published.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from ossbot.common.clock import to_iso
from ossbot.common.config import TelegraphConfig
from ossbot.common.hashing import sha256_hex
from ossbot.gatekeeper import db, policy
from ossbot.gatekeeper.telegraph_writer import GATEKEEPER_PROXY, Page, TelegraphError, TelegraphWriter, make_client

if TYPE_CHECKING:
    from ossbot.gatekeeper.context import AppContext

log = logging.getLogger(__name__)
T = TypeVar("T")
ALLOWED_TAGS = frozenset({"p", "h3", "h4", "ul", "ol", "li", "pre", "code", "blockquote", "b", "i", "s", "u", "hr",
                          "br", "a"})  # fmt: skip
PRIVATE_TOOLS = frozenset({"memory", "session_search"})
REMOVED_TITLE = "Removed"
REMOVED_TEXT = "This page was removed by its author."
NOT_ELIGIBLE = "Not published: this answer is no longer eligible"
REFUSED_STATE = "Paused or killed: nothing was published."
REFUSED_UNPUBLISH = "Paused or killed: the page was not changed and is still public. /resume first."
_FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
_LIST = re.compile(r"^\s{0,3}([-*+]|\d{1,3}[.)])\s+(.*)$")
_INLINE = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|\*[^*\n]+\*|_[^_\n]+_"
                     r"|\[[^\]\n]+\]\([^)\s]+\))")  # fmt: skip


# ------------------------------------------------------------------------------- state
def token(ctx: AppContext) -> str | None:
    return ctx.env.get("TELEGRAPH_ACCESS_TOKEN") or None


def system_running(ctx: AppContext) -> bool:
    """Paused or killed refuse every Telegraph write; draining and DOWN (workers) don't (gatekeeper-only)."""
    return ctx.control.view().state == "running"


def turn_row(ctx: AppContext, turn_id: int) -> Any:
    return db.one(ctx.conn, "SELECT * FROM chat_turns WHERE turn_id = ?", (turn_id,))


def check(ctx: AppContext, turn_id: int) -> str | None:
    """The call sites' entry (render, tap, Confirm, before createPage): an account must exist, then
    ``eligible`` on a freshly read ``chat_turns`` row."""
    if not token(ctx):
        return "no Telegraph account (ossctl telegraph setup)"
    from ossbot.gatekeeper.telegram.runtime import ui_of  # noqa: PLC0415 - telegram imports the context

    reviews = ui_of(ctx).topics.thread_id_raw("reviews")
    return eligible(turn_row(ctx, turn_id), ctx.config.telegram.telegraph, reviews)


def eligible(turn: Any, cfg: TelegraphConfig, reviews_thread_id: int | None) -> str | None:
    """BA-19, the only eligibility check: None = eligible, else the failing rule (for the INFO log)."""
    if not cfg.enabled:
        return "Telegraph is off"
    if turn is None or turn["status"] not in {"done", "stopped"} or not turn["answer_text"]:
        return "no stored answer"
    text = str(turn["answer_text"])
    if len(text) < cfg.offer_min_chars:
        return "the answer is short"
    if len(json.dumps(to_nodes(text), ensure_ascii=False).encode("utf-8")) > cfg.max_content_kb * 1024:
        return "the answer is too long for Telegraph"
    if turn["had_attachments"]:
        return "the turn had attachments"
    try:
        tools = [str(t) for t in json.loads(turn["tools_json"] or "[]")]
    except ValueError:
        return "unknown tools"
    if any(t in PRIVATE_TOOLS or t.startswith("ossbot_") for t in tools):
        return "the turn used memory, session search or ossbot tools"
    reviews = reviews_thread_id
    if str(turn["session_key"]).endswith("-reviews") or (reviews is not None and turn["thread_id"] == reviews):
        return "a Reviews answer"
    if int(turn["feature_cards"] or 0):
        return "the turn produced a feature card"
    return None


# ------------------------------------------------------------------------------- content
async def scan(ctx: AppContext, text: str) -> str | None:
    """The class of the first hit, or None. No override exists. ``gitleaks stdin`` runs in a thread."""
    if ctx.secret_scanner.scan(text):
        return "a secret"
    from ossbot.gatekeeper.packaging import gitleaks_scan  # noqa: PLC0415 - heavy module, only here

    if await asyncio.to_thread(ctx.gitleaks_scanner or gitleaks_scan, text.encode("utf-8")):
        return "a secret (gitleaks)"
    lowered = text.lower()
    if any(v and v in lowered for v in policy.effective_confidentiality(ctx.conn, ctx.config)):
        return "a confidential value"
    profile = ctx.config.profile
    if profile.commit_email.lower() in lowered or profile.commit_name.lower() in lowered:
        return "personal details"
    return None


def _inline(text: str) -> list[Any]:
    out: list[Any] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(text[pos : m.start()])
        tok = m.group(0)
        if tok.startswith("`"):
            out.append({"tag": "code", "children": [tok[1:-1]]})
        elif tok.startswith(("**", "__")):
            out.append({"tag": "b", "children": _inline(tok[2:-2])})
        elif tok.startswith("~~"):
            out.append({"tag": "s", "children": _inline(tok[2:-2])})
        elif tok.startswith("["):
            label, url = tok[1:].split("](", 1)
            url = url[:-1]
            if url.startswith(("https://", "http://")):
                out.append({"tag": "a", "attrs": {"href": url}, "children": [label]})
            else:
                out.append(label)
        else:
            out.append({"tag": "i", "children": _inline(tok[1:-1])})
        pos = m.end()
    if pos < len(text):
        out.append(text[pos:])
    return out


def _lines_inline(lines: list[str]) -> list[Any]:
    out: list[Any] = []
    for i, ln in enumerate(lines):
        if i:
            out.append({"tag": "br"})
        out += _inline(ln.strip())
    return out


def to_nodes(markdown: str) -> list[dict[str, Any]]:
    """Markdown -> Telegraph nodes with ``ALLOWED_TAGS`` only (no media); tables become ``pre`` text."""
    nodes: list[dict[str, Any]] = []
    lines = markdown.replace("\r\n", "\n").split("\n")
    i = 0
    para: list[str] = []

    def flush() -> None:
        if para:
            nodes.append({"tag": "p", "children": _lines_inline(para)})
            para.clear()

    while i < len(lines):
        ln = lines[i]
        fence = _FENCE.match(ln)
        if fence:
            flush()
            closer = fence.group(1)[:3]
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(closer):
                body.append(lines[i])
                i += 1
            nodes.append({"tag": "pre", "children": ["\n".join(body)]})
            i += 1
            continue
        stripped = ln.strip()
        if not stripped:
            flush()
        elif stripped.startswith("#"):
            flush()
            level = len(stripped) - len(stripped.lstrip("#"))
            nodes.append({"tag": "h3" if level <= 1 else "h4", "children": _inline(stripped.lstrip("#").strip())})
        elif re.fullmatch(r"[-*_]{3,}", stripped):
            flush()
            nodes.append({"tag": "hr"})
        elif stripped.startswith("|"):
            flush()
            table: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table.append(lines[i].strip())
                i += 1
            nodes.append({"tag": "pre", "children": ["\n".join(table)]})
            continue
        elif stripped.startswith(">"):
            flush()
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            nodes.append({"tag": "blockquote", "children": _lines_inline(quote)})
            continue
        elif _LIST.match(ln):
            flush()
            ordered = _LIST.match(ln).group(1)[0].isdigit()  # type: ignore[union-attr]
            items: list[dict[str, Any]] = []
            while i < len(lines) and (m := _LIST.match(lines[i])):
                items.append({"tag": "li", "children": _inline(m.group(2).strip())})
                i += 1
            nodes.append({"tag": "ol" if ordered else "ul", "children": items})
            continue
        else:
            para.append(ln)
        i += 1
    flush()
    return nodes


def tags_of(nodes: list[Any]) -> set[str]:
    out: set[str] = set()
    for n in nodes:
        if isinstance(n, dict):
            out.add(str(n.get("tag")))
            out |= tags_of(list(n.get("children") or []))
    return out


def make_title(markdown: str) -> str:
    """The first heading (or the first 60 characters) + `` · `` + 8 random base32 characters."""
    first = next((ln.strip().lstrip("#").strip() for ln in markdown.split("\n") if ln.strip().startswith("#")), "")
    base = first or " ".join(markdown.split())
    base = re.sub(r"[`*_~\[\]]", "", base)[:60].strip() or "Answer"
    suffix = base64.b32encode(secrets.token_bytes(5)).decode("ascii")[:8]
    return f"{base} · {suffix}"


# ------------------------------------------------------------------------------- the service
@dataclass
class PublishTaskState:
    """BA-20: one per ``telegraph`` task, shared by the task and its ``on_done``. ``request_started`` is set
    right before the shielded request; ``outer_cancelled`` when a stop reached the outer task."""

    request_started: bool = False
    outer_cancelled: bool = False


class Telegraph:
    """``ctx.telegraph``: the writer plus the publish and unpublish runs (spawned as ``telegraph`` tasks)."""

    def __init__(self, ctx: AppContext, writer: TelegraphWriter) -> None:
        self.ctx = ctx
        self.writer = writer
        self.inflight: set[asyncio.Task[Any]] = set()

    async def publish(self, turn_id: int, run: PublishTaskState,
                      announce: Callable[[Page, str, bool], None]) -> tuple[str, Page | str]:  # fmt: skip
        """("published", page) or ("refused", text). The request, the page row and the announcement are
        shielded: a stop during ``createPage`` still records the page and announces it (after stop)."""
        ctx = self.ctx
        if check(ctx, turn_id) is not None:
            return "refused", NOT_ELIGIBLE
        text = str(turn_row(ctx, turn_id)["answer_text"])  # rebuilt from the stored, redacted answer
        hit = await scan(ctx, text)
        if hit is not None:
            return "refused", f"Not published: the answer contains {hit}"
        nodes = to_nodes(text)
        if not tags_of(nodes) <= ALLOWED_TAGS:  # pragma: no cover - to_nodes emits only allowed tags
            return "refused", "Not published: unexpected content"
        title = make_title(text)
        if not system_running(ctx):
            return "refused", "Not published (paused or killed)."
        if check(ctx, turn_id) is not None:  # right before createPage (AC 65)
            return "refused", NOT_ELIGIBLE
        run.request_started = True
        page = await asyncio.shield(self._track(self._create(turn_id, title, nodes, text, run, announce)))
        return "published", page

    def _track(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """The shielded request as a task in ``inflight``: ``Handlers.drain`` awaits it at shutdown (F6-4)."""
        task = asyncio.ensure_future(coro)
        self.inflight.add(task)
        task.add_done_callback(self.inflight.discard)
        return task

    async def _create(self, turn_id: int, title: str, nodes: list[Any], text: str, run: PublishTaskState,
                      announce: Callable[[Page, str, bool], None]) -> Page:  # fmt: skip
        page = await self.writer.create_page(title, nodes, self.ctx.config.telegram.telegraph.author_name)
        with db.transaction(self.ctx.conn):
            self.ctx.conn.execute(
                "INSERT INTO telegraph_pages (path, url, title, turn_id, content_sha256, published_at)"
                " VALUES (?,?,?,?,?,?)",
                (page.path, page.url, title, turn_id, sha256_hex(text.encode("utf-8")), to_iso(self.ctx.clock.now())),
            )  # fmt: skip
            self.ctx.audit.append("telegram", "telegraph_published", {"path": page.path, "turn_id": turn_id})
        announce(page, title, run.outer_cancelled)
        return page

    async def unpublish(self, path: str, run: PublishTaskState, announce: Callable[[bool], None]) -> str | None:
        """None when the page was blanked (the shielded section announced it), else the refusal text."""
        if not system_running(self.ctx):
            return REFUSED_UNPUBLISH
        row = db.one(self.ctx.conn, "SELECT * FROM telegraph_pages WHERE path = ?", (path,))
        if row is None or row["unpublished_at"] is not None:
            return "Already unpublished."
        run.request_started = True
        await asyncio.shield(self._track(self._blank(path, run, announce)))
        return None

    async def _blank(self, path: str, run: PublishTaskState, announce: Callable[[bool], None]) -> None:
        await self.writer.edit_page(path, REMOVED_TITLE, [{"tag": "p", "children": [REMOVED_TEXT]}])
        with db.transaction(self.ctx.conn):
            self.ctx.conn.execute("UPDATE telegraph_pages SET unpublished_at = ? WHERE path = ?",
                                  (to_iso(self.ctx.clock.now()), path))  # fmt: skip
            self.ctx.audit.append("telegram", "telegraph_unpublished", {"path": path})
        announce(run.outer_cancelled)


async def create_account(*, direct: bool = True) -> str:
    """``ossctl telegraph setup`` (root): ``createAccount`` with ``short_name: ossbot``; returns the token."""
    async with make_client(proxy=None if direct else GATEKEEPER_PROXY) as client:
        return await TelegraphWriter(client, None).create_account("ossbot")


async def rotate_token(current: str, *, direct: bool = True) -> str:
    """``ossctl telegraph rotate`` (root): ``revokeAccessToken``; returns the new token."""
    async with make_client(proxy=None if direct else GATEKEEPER_PROXY) as client:
        return await TelegraphWriter(client, current).revoke()


def pages(ctx: AppContext) -> list[Any]:
    return list(ctx.conn.execute("SELECT * FROM telegraph_pages WHERE unpublished_at IS NULL ORDER BY published_at"
                                 " DESC LIMIT 20").fetchall())  # fmt: skip


__all__ = ["NOT_ELIGIBLE", "REFUSED_STATE", "REFUSED_UNPUBLISH", "Page", "PublishTaskState", "Telegraph",
           "TelegraphError", "TelegraphWriter", "check", "create_account", "eligible", "make_client", "make_title",
           "pages", "rotate_token", "scan", "system_running", "tags_of", "to_nodes", "token", "turn_row"]  # fmt: skip
