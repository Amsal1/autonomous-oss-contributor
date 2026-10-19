"""The 🌐 HTML diff document (DESIGN §14.11.4; AC 23).

Jinja2 with ``autoescape=True`` and no ``|safe`` anywhere in the template (a unit test parses the
template AST); inline CSS, no script, a CSP meta that allows nothing but inline styles and data images;
light and dark via ``prefers-color-scheme``. Built only from ``derived/<cid>/<rev>/`` (U4): the diff
code is publish bytes, so the caller scans it and sends nothing on a hit (U14). Capped at 10 MB with a
truncation note per file beyond that.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Any

from jinja2 import Environment, StrictUndefined

from ossbot.gatekeeper.telegram.review.patchview import FilePatch

MAX_BYTES = 10 * 1024 * 1024
TEMPLATE = "review.html.j2"


@dataclass(frozen=True)
class Row:
    old: int | None
    new: int | None
    sign: str
    text: str


def template_source() -> str:
    return (resources.files("ossbot.gatekeeper") / "templates" / TEMPLATE).read_text(encoding="utf-8")


def environment() -> Environment:
    return Environment(autoescape=True, undefined=StrictUndefined, keep_trailing_newline=True)


def render(*, cid: str, rev: int, upstream: str, facts: list[str], files: list[FilePatch], title: str, body: str,
           commit_message: str) -> bytes:  # fmt: skip
    budget = MAX_BYTES - 200_000
    views: list[dict[str, Any]] = []
    for fp in files:
        rows: list[Row] = []
        for hunk in fp.hunks:
            rows.append(Row(None, None, "@", hunk.header))
            for old, new, text in hunk.numbered():
                rows.append(Row(old, new, text[:1], text[1:]))
        cost = sum(len(r.text) + 40 for r in rows)
        truncated = cost > budget
        if truncated:
            rows = rows[: max(0, budget // 80)]
        budget -= min(cost, budget)
        views.append({"path": fp.path, "added": fp.added, "deleted": fp.deleted, "rows": rows,
                      "truncated": truncated, "binary": fp.binary})  # fmt: skip
    html = environment().from_string(template_source()).render(
        cid=cid, rev=rev, upstream=upstream, facts=facts, files=views, title=title, body=body,
        commit_message=commit_message,
    )  # fmt: skip
    return html.encode("utf-8")[:MAX_BYTES]
