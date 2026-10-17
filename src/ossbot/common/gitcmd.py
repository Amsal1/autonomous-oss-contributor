"""Fixed git commands shared by the sandbox package job and the gatekeeper (DESIGN §9.11, M7, R4-N6).

Both strings are the single source: the ``sandbox-full`` image renders them into
``OSSBOT_FORMAT_PATCH``/``OSSBOT_NUMSTAT`` and the gatekeeper runs the same argv, so the two
sides cannot diverge. Usage: ``git <hardened -c flags> $FORMAT_PATCH <round_base>..<head>`` and
``git <hardened -c flags> $NUMSTAT <round_base> <head>``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FORMAT_PATCH = (
    "-c diff.algorithm=myers -c diff.renames=false -c core.quotePath=true format-patch --stdout --no-signature"
    " --no-stat --full-index --no-ext-diff --no-textconv --no-color"
)
NUMSTAT = (
    "-c diff.algorithm=myers -c diff.renames=false -c core.quotePath=true diff --numstat --no-ext-diff --no-textconv"
    " --no-color"
)
FORMAT_PATCH_ARGV: tuple[str, ...] = tuple(FORMAT_PATCH.split())
NUMSTAT_ARGV: tuple[str, ...] = tuple(NUMSTAT.split())

# The package job always commits on this branch; bundles carry exactly this one ref (M1).
CHANGE_BRANCH = "ossbot-change"
CHANGE_REF = f"refs/heads/{CHANGE_BRANCH}"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_NUMSTAT_LINE = re.compile(r"^(-|\d+)\t(-|\d+)\t(.+)$")


@dataclass(frozen=True, order=True)
class NumstatEntry:
    """One numstat line; ``added``/``deleted`` are None for binary files (``-``)."""

    added: int | None
    deleted: int | None
    path: str


def parse_numstat(text: str) -> list[NumstatEntry]:
    """Parse ``git diff --numstat`` output (no ``-z``; paths quoted per ``core.quotePath``)."""
    out: list[NumstatEntry] = []
    for line in text.splitlines():
        if not line:
            continue
        match = _NUMSTAT_LINE.match(line)
        if match is None:
            raise ValueError(f"unparseable numstat line: {line[:200]!r}")
        added, deleted, path = match.groups()
        out.append(NumstatEntry(None if added == "-" else int(added), None if deleted == "-" else int(deleted), path))
    return out
