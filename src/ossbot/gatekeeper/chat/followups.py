"""Follow-up suggestions (DESIGN §14.12.4, D9, U2, U14).

Hermes may end an answer with a fenced ```` ```followups ```` block. ``extract`` removes it from the
answer and keeps at most three items. An item is **dropped, never cut** when it hides characters
(controls, invisible or bidi), starts with ``/``, names an approval or control word, is longer than 64
characters after collapsing whitespace, or carries anything ``redact()`` would replace: the label is
the exact text a tap sends, so what the user sees is what Hermes gets.
"""

from __future__ import annotations

import re

from ossbot.gatekeeper.telegram.escape import make_visible
from ossbot.gatekeeper.telegram.ui.limits import LABEL_SINGLE_MAX, label_len
from ossbot.gatekeeper.telegram.ui.redact import redact

MAX_ITEMS = 3
FORBIDDEN = re.compile(r"approve|confirm|submit|resume|dry", re.IGNORECASE)
_BLOCK = re.compile(r"(?:^|\n)[ \t]*```followups[ \t]*\n(?P<body>.*?)(?:\n[ \t]*```[ \t]*(?=\n|$)|\Z)", re.DOTALL)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d{1,2}[.)])\s+")
_WS = re.compile(r"\s+")


def clean(item: str) -> str | None:
    """One suggestion -> its label, or None when it must be dropped."""
    _, hidden = make_visible(item)
    if hidden:
        return None
    label = _WS.sub(" ", _BULLET.sub("", item)).strip()
    if not label or label.startswith("/") or FORBIDDEN.search(label):
        return None
    if label_len(label) > LABEL_SINGLE_MAX:
        return None
    if redact(label, surface="followup")[1]:
        return None
    return label


def extract(text: str) -> tuple[str, list[str]]:
    """(answer without the followups block, ≤ 3 labels). Only the last block counts; any earlier one is
    removed too, so no block text is ever shown as part of the answer."""
    matches = list(_BLOCK.finditer(text))
    if not matches:
        return text, []
    body = matches[-1].group("body")
    stripped = _BLOCK.sub("\n", text).rstrip()
    labels: list[str] = []
    for raw in body.splitlines():
        if not raw.strip():
            continue
        label = clean(raw)
        if label is not None and label not in labels:
            labels.append(label)
        if len(labels) == MAX_ITEMS:
            break
    return stripped, labels
