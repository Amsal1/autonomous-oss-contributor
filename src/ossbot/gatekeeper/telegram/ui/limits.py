"""Telegram limits and the ossbot budgets (DESIGN §14.17.2, §14.17.4), plus the packers.

Every send path checks against these constants; ``tests/telegram/test_kit_limits.py`` property-tests
them over every View builder.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from ossbot.gatekeeper.telegram.escape import esc, make_visible

CLASSIC_MAX = 3900  # escaped characters (Bot API 4096 after entity parsing)
PART_MAX = 3500  # one escaped <pre> chunk
RICH_MAX_CHARS = 30_000  # Bot API 32,768
RICH_MAX_BLOCKS = 450  # Bot API 500
RICH_MAX_DEPTH = 8  # Bot API 16
RICH_MAX_COLUMNS = 12
RICH_PRE_MAX = 12_000
CAPTION_MAX = 900  # Bot API 1,024
CALLBACK_DATA_BYTES = 24  # always "t:" + 22
CALLBACK_ANSWER_MAX = 180  # Bot API 0-200
LABEL_MULTI_TARGET = 20
LABEL_MULTI_MAX = 24  # rows with two or more buttons
LABEL_SINGLE_MAX = 64  # single-button rows
BUTTONS_PER_ROW = 3
BUTTONS_PER_ROW_LONG = 2  # when any label in the row is > 12
LONG_LABEL = 12
ROWS_MAX = 6
CARD_ROWS_MAX = 4
PICKER_ROWS_MAX = 5  # plus one pager row
PLACEHOLDER_MAX = 60  # ForceReply input_field_placeholder (Bot API 1-64)
COPY_TEXT_MAX = 256
TOPIC_NAME_MAX = 48
COMMAND_DESC_MAX = 60
SHORT_DESCRIPTION_MAX = 100  # Bot API 120
DESCRIPTION_MAX = 200  # Bot API 512
DRAFT_TAIL_MAX = 3500
ELLIPSIS = "…"


class LimitError(ValueError):
    """A constant label, placeholder or keyboard shape breaks the §14.17 rules (a bug in code)."""


def label_len(label: str) -> int:
    """Code points after NFC; an emoji with VS16 counts 2 (§14.17.2)."""
    return len(unicodedata.normalize("NFC", label))


def check_row(labels: Sequence[str]) -> None:
    """The one label rule, asserted per row: multi-button rows <= 24 (<= 2 buttons when any label is
    longer than 12), single-button rows <= 64, at most 3 buttons."""
    if not labels:
        raise LimitError("empty keyboard row")
    if len(labels) > BUTTONS_PER_ROW:
        raise LimitError(f"{len(labels)} buttons in a row (max {BUTTONS_PER_ROW})")
    lengths = [label_len(lb) for lb in labels]
    if min(lengths) == 0:
        raise LimitError("empty button label")
    if len(labels) == 1:
        if lengths[0] > LABEL_SINGLE_MAX:
            raise LimitError(f"label {labels[0]!r} is {lengths[0]} > {LABEL_SINGLE_MAX}")
        return
    if max(lengths) > LABEL_MULTI_MAX:
        raise LimitError(f"label in a {len(labels)}-button row is {max(lengths)} > {LABEL_MULTI_MAX}: {labels}")
    if max(lengths) > LONG_LABEL and len(labels) > BUTTONS_PER_ROW_LONG:
        raise LimitError(f"{len(labels)} buttons with a label > {LONG_LABEL}: {labels}")


def check_keyboard(rows: Sequence[Sequence[str]], *, max_rows: int = ROWS_MAX) -> None:
    if len(rows) > max_rows:
        raise LimitError(f"{len(rows)} rows (max {max_rows})")
    for row in rows:
        check_row(row)


def check_placeholder(text: str) -> str:
    if not 1 <= label_len(text) <= PLACEHOLDER_MAX:
        raise LimitError(f"placeholder {text!r} not 1-{PLACEHOLDER_MAX} characters")
    return text


def cut(text: str, limit: int) -> str:
    """Display cut with "…" (never for publish bytes)."""
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + ELLIPSIS


def middle_cut(name: str, limit: int) -> str:
    """Names (repo, file, topic) keep both ends: ``src/very/…/file.py``."""
    if len(name) <= limit:
        return name
    keep = max(2, limit - 1)
    head = keep // 2
    return name[:head] + ELLIPSIS + name[len(name) - (keep - head) :]


def escaped_len(text: str) -> int:
    return len(esc(make_visible(text)[0]))


def _cost(ch: str) -> int:
    return len(esc(make_visible(ch)[0]))  # both passes are per character, so the cost is additive


def chunk_verbatim(text: str, limit: int = PART_MAX) -> list[str]:
    """Split ``text`` so every chunk's escaped, visible form is <= ``limit``; line boundaries first, a
    hard split inside a line only when the line alone is too long. Joining the chunks gives ``text``."""
    out: list[str] = []
    cur: list[str] = []
    cur_cost = 0

    def flush() -> None:
        nonlocal cur, cur_cost
        if cur:
            out.append("".join(cur))
        cur, cur_cost = [], 0

    for line in text.splitlines(keepends=True):
        line_cost = sum(_cost(ch) for ch in line)
        if line_cost <= limit:
            if cur_cost + line_cost > limit:
                flush()
            cur.append(line)
            cur_cost += line_cost
            continue
        for ch in line:
            c = _cost(ch)
            if cur_cost + c > limit:
                flush()
            cur.append(ch)
            cur_cost += c
    flush()
    return out or [""]
