"""One frame -> a static HTML page (no script; CSP ``default-src 'none'``).

Message text and buttons come only from the captured payloads (``tghtml``/``richhtml``); everything
else on the page is marked ``data-chrome`` (status bar, header, pinned bar, times, reactions, caption),
which is what ``checks.assert_faithful`` relies on.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any, Literal

from demokit import richhtml, tghtml
from demokit.chatstate import Frame, Message

TEMPLATES = Path(__file__).resolve().parent / "templates"
Theme = Literal["light", "dark"]
Layout = Literal["still", "gif"]
PAGE_SIZE: dict[str, tuple[int, int]] = {"still": (384, 804), "gif": (720, 820)}
SNIPPET_MAX = 90
BRAND = "ossbot · rendered from captured gatekeeper payloads"


@dataclass(frozen=True)
class Caption:
    step: str  # "1 / 9"
    title: str
    subtitle: str
    index: int  # 0-based scene index
    total: int


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _attr(text: str) -> str:
    return html.escape(text, quote=True)


def visible_text(msg: Message) -> str:
    """What Telegram shows as the message text."""
    if msg.kind == "user":
        return msg.text
    if msg.kind == "rich":
        assert msg.rich is not None
        return richhtml.plain_text(msg.rich)
    return tghtml.plain_text(msg.text)


def first_line(msg: Message) -> str:
    line = next((ln for ln in visible_text(msg).split("\n") if ln.strip()), "")
    return line.strip()[:SNIPPET_MAX]


def _body(msg: Message) -> str:
    if msg.kind == "user":
        return _esc(msg.text)
    if msg.kind == "rich":
        assert msg.rich is not None
        return richhtml.to_html(msg.rich)
    return tghtml.to_html(msg.text)


def _clock(at: str) -> str:
    return datetime.fromisoformat(at.replace("Z", "+00:00")).strftime("%H:%M")


def _button(btn: dict[str, Any], pressed: str | None) -> str:
    label = str(btn.get("text", ""))
    classes = ["btn"]
    style = btn.get("style")
    if style in {"success", "danger", "primary"}:
        classes.append(str(style))
    if "disabled" in btn:
        classes.append("disabled")
    if "url" in btn:
        classes.append("url")
    if pressed is not None and label.startswith(pressed):
        classes.append("pressed")
    return f'<div class="{" ".join(classes)}">{_esc(label)}</div>'


def _keyboard(msg: Message, pressed: str | None) -> str:
    rows = (msg.markup or {}).get("inline_keyboard") or []
    if not rows:
        return ""
    out = ['<div class="kb">']
    for row in rows:
        out.append('<div class="kb-row">' + "".join(_button(b, pressed) for b in row) + "</div>")
    out.append("</div>")
    return "".join(out)


def _message(msg: Message, frame: Frame, pressed: str | None) -> str:
    side = "out" if msg.outgoing else "in"
    classes = f"msg {side}" + (" draft" if msg.draft else "")
    parts = [f'<div class="{classes}" data-key="{msg.key}" data-seq="{msg.seq}" data-payload-sha256="{msg.sha256()}">',
             '<div class="bubble">']  # fmt: skip
    if msg.reply_to is not None and msg.reply_to in frame.state.messages:
        ref = frame.state.messages[msg.reply_to]
        parts.append(f'<div class="reply" data-chrome="reply" data-ref="{ref.key}">{_esc(first_line(ref))}</div>')
    parts.append(f'<div class="msg-text {msg.kind}">{_body(msg)}</div>')
    meta = "streaming…" if msg.draft else _clock(msg.at)
    parts.append(f'<div class="meta" data-chrome="time">{_esc(meta)}</div>')
    parts.append("</div>")
    if msg.reactions:
        parts.append(f'<div class="reactions" data-chrome="reactions">{_esc(" ".join(msg.reactions))}</div>')
    parts.append(_keyboard(msg, pressed))
    parts.append("</div>")
    return "".join(parts)


def _pinned(frame: Frame) -> str:
    if frame.pinned is None:
        return ""
    return (f'<div class="pinned" data-chrome="pinned" data-ref="{frame.pinned.key}"><span class="pin-body">'
            f'<span class="pin-title">Pinned message</span><span class="pin-text">{_esc(first_line(frame.pinned))}'
            "</span></span></div>")  # fmt: skip


def _caption(caption: Caption | None) -> str:
    if caption is None:
        return ""
    dots = "".join(f'<span class="cap-dot{" on" if i == caption.index else ""}"></span>' for i in range(caption.total))
    return (f'<div class="caption" data-chrome="caption"><div class="cap-step">{_esc(caption.step)}</div>'
            f'<div class="cap-title">{_esc(caption.title)}</div><div class="cap-sub">{_esc(caption.subtitle)}</div>'
            f'<div class="cap-dots">{dots}</div></div>'
            f'<div class="cap-brand" data-chrome="caption">{_esc(BRAND)}</div>')  # fmt: skip


def render_page(frame: Frame, *, theme: Theme, layout: Layout, caption: Caption | None = None) -> str:
    if layout == "gif" and caption is None:
        raise ValueError("the gif layout needs a caption")
    messages = "\n".join(_message(m, frame, frame.mark.pressed) for m in frame.messages)
    aria = f"Telegram chat with ossbot, topic {frame.topic or 'chat'}"
    page = Template((TEMPLATES / "frame.html").read_text(encoding="utf-8")).substitute(
        css=(TEMPLATES / "frame.css").read_text(encoding="utf-8").strip(), theme=theme, layout=layout,
        aria=_attr(aria), topic=_esc(frame.topic or "Chat"), pinned=_pinned(frame), messages=messages,
        caption=_caption(caption) if layout == "gif" else "",
    )  # fmt: skip
    return page
