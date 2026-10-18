"""Change-size classes and numstat measurement (DESIGN §9.14, §12.6; CR-004 Amendment B).

Size classes are estimates for what the bot picks on its own; they never block the user. The
hard ceiling (``policy.diff``) is checked by ``gatekeeper.policy.check_diff`` with the same split:

* ``exclude_globs`` (lockfiles, generated, vendored) drop a path from both line totals, but the path
  still counts as a changed file (decision B5: a runaway agent rewriting lockfiles still hits
  ``max_files``);
* ``test_globs`` move a path's lines to the test totals, which never count toward the class.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Protocol

SizeClass = Literal["xs", "s", "m", "l", "xl", "xxl"]
# xxl is "above xl": never selectable as a preference (DESIGN §9.14).
SelectableSizeClass = Literal["xs", "s", "m", "l", "xl"]
SIZE_CLASSES: tuple[SizeClass, ...] = ("xs", "s", "m", "l", "xl", "xxl")
# class -> (max non-test changed lines, max files); xxl has no upper bound.
CLASS_BOUNDS: dict[SelectableSizeClass, tuple[int, int]] = {
    "xs": (20, 2),
    "s": (80, 4),
    "m": (250, 8),
    "l": (500, 15),
    "xl": (1000, 30),
}


class NumstatEntry(Protocol):
    """One changed path; ``added``/``deleted`` are None for binary files."""

    @property
    def path(self) -> str: ...
    @property
    def added(self) -> int | None: ...
    @property
    def deleted(self) -> int | None: ...


@dataclass(frozen=True)
class SizeMeasure:
    files: int
    code_added: int
    code_deleted: int
    test_added: int
    test_deleted: int

    @property
    def code_lines(self) -> int:
        return self.code_added + self.code_deleted

    @property
    def test_lines(self) -> int:
        return self.test_added + self.test_deleted


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """gitignore-style: a pattern without '/' matches the basename at any depth; '**' spans dirs."""
    anchored = "/" in pattern.rstrip("/")
    pat = pattern.lstrip("/")
    out = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    body = "".join(out)
    return re.compile(f"^{body}$" if anchored else f"^(?:.*/)?{body}$")


def path_matches(path: str, patterns: Iterable[str]) -> str | None:
    """The first pattern matching ``path``, or None."""
    norm = path.lstrip("/")
    for pattern in patterns:
        if _glob_regex(pattern).match(norm):
            return pattern
    return None


def split_numstat(
    entries: Sequence[NumstatEntry], test_globs: Sequence[str], exclude_globs: Sequence[str]
) -> SizeMeasure:
    """Split numstat into code and test lines; excluded paths count as files but add no lines."""
    code_added = code_deleted = test_added = test_deleted = 0
    for e in entries:
        if path_matches(e.path, exclude_globs):
            continue
        added, deleted = e.added or 0, e.deleted or 0
        if path_matches(e.path, test_globs):
            test_added += added
            test_deleted += deleted
        else:
            code_added += added
            code_deleted += deleted
    return SizeMeasure(len(entries), code_added, code_deleted, test_added, test_deleted)


def classify(code_lines: int, files: int) -> SizeClass:
    """The smallest class whose line and file bounds both hold; ``xxl`` above ``xl``."""
    for name, (max_lines, max_files) in CLASS_BOUNDS.items():
        if code_lines <= max_lines and files <= max_files:
            return name
    return "xxl"
