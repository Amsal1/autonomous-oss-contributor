"""The redaction choke point before any send (DESIGN §14.9 U14, BA-16).

Every untrusted or model-authored text leaf goes through ``redact`` before it reaches Telegram or a
stored answer; drafts, Stop partials and truncated answers are built only with ``draft_visible``,
so a secret's prefix is never shown while a stream is still arriving. Verbatim publish bytes (PR
title, body/commit/comment parts, ``.patch``) are passed to ``scan`` and never altered: a hit there
is a bug (packaging already refused it) and is logged at ERROR.

The scanner is the gatekeeper's ``SecretScanner`` of the current snapshot (``bind`` is called by
``build_context`` with ``lambda: ctx.secret_scanner``); unbound, the default token patterns apply.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ossbot.gatekeeper.secret_scan import HOLD_BACK, WHITESPACE, SecretScanner

log = logging.getLogger(__name__)
_DEFAULT = SecretScanner()
_provider: Callable[[], SecretScanner] = lambda: _DEFAULT  # noqa: E731


def bind(provider: Callable[[], SecretScanner]) -> None:
    global _provider  # noqa: PLW0603 - one scanner per gatekeeper process
    _provider = provider


def redact(text: str, *, surface: str = "text") -> tuple[str, int]:
    """Display text: every secret value, token pattern and Kiro key becomes ``[REDACTED]``."""
    out, hits = _provider().redact(text)
    if hits:
        log.warning("redacted %d hit(s) on %s", hits, surface, extra={"event": "redacted", "surface": surface,
                                                                       "hits": hits})  # fmt: skip
    return out, hits


def scan(text: str | bytes, *, surface: str = "verbatim") -> int:
    """Verbatim publish bytes: counted, never altered. A hit is logged at ERROR (never the match)."""
    count = _provider().scan(text)
    if count:
        log.error("verbatim text on %s carries %d secret hit(s)", surface, count,
                  extra={"event": "redaction_verbatim_hit", "surface": surface, "count": count})  # fmt: skip
    return count


def draft_visible(text: str) -> str:
    """The visible part of a stream that has not completed: redact the whole text, then cut at
    ``max(last whitespace index, len - HOLD_BACK)``. Values are whitespace-free and shorter than
    ``HOLD_BACK`` and patterns can't match whitespace (load refusals), so an incomplete secret at the
    tail always starts after the cut: no prefix of it is ever shown."""
    redacted, _ = redact(text, surface="draft")
    last_ws = max((redacted.rfind(ch) for ch in WHITESPACE), default=-1)
    cut = max(last_ws, len(redacted) - HOLD_BACK, 0)
    return redacted[:cut]
