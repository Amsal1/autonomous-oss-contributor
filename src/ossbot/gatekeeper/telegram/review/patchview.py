"""The derived patch as files and hunks for the explorer and file views (DESIGN §14.11.2-§14.11.3; U4).

Reads only ``derived/<cid>/<rev>/change.patch`` (the gatekeeper's own ``FORMAT_PATCH`` output); display
artifacts are never bound or trusted. Each rendered page keeps a line map (old/new line numbers per
shown line) so a quote-reply can be anchored to lines (§14.11.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
PAGE_CHARS = 3300  # escaped characters of one classic file page (inside <pre>, ≤ 3,500)


@dataclass
class Hunk:
    header: str
    lines: list[str] = field(default_factory=list)
    old_start: int = 0
    new_start: int = 0

    def numbered(self) -> list[tuple[int | None, int | None, str]]:
        """(old line, new line, text) per hunk line."""
        out: list[tuple[int | None, int | None, str]] = []
        old, new = self.old_start, self.new_start
        for text in self.lines:
            if text.startswith("+"):
                out.append((None, new, text))
                new += 1
            elif text.startswith("-"):
                out.append((old, None, text))
                old += 1
            else:
                out.append((old, new, text))
                old += 1
                new += 1
        return out

    def new_range(self) -> tuple[int, int]:
        nums = [n for _, n, t in self.numbered() if n is not None and t.startswith("+")]
        return (min(nums), max(nums)) if nums else (self.new_start, self.new_start)


@dataclass
class FilePatch:
    path: str
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def added(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.startswith("+"))

    @property
    def deleted(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.startswith("-"))


def parse(patch: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    for line in patch.splitlines():
        m = DIFF_RE.match(line)
        if m:
            current = FilePatch(m.group(2))
            files.append(current)
            hunk = None
            continue
        if current is None:
            continue
        if line.startswith("Binary files"):
            current.binary = True
            continue
        h = HUNK_RE.match(line)
        if h:
            hunk = Hunk(line, old_start=int(h.group(1)), new_start=int(h.group(3)))
            current.hunks.append(hunk)
            continue
        if hunk is not None and line[:1] in {"+", "-", " "} and not line.startswith(("+++ ", "--- ")):
            hunk.lines.append(line)
    return files


def load(derived_dir: Path) -> list[FilePatch]:
    try:
        return parse((derived_dir / "change.patch").read_bytes().decode("utf-8", "replace"))
    except OSError:
        return []


@dataclass(frozen=True)
class Page:
    text: str
    lines: tuple[tuple[int | None, int | None, str], ...]  # the line map of what is shown
    first_hunk: int
    last_hunk: int


def pages(fp: FilePatch, limit: int = PAGE_CHARS) -> list[Page]:
    """The file's hunks as pages of ≤ ``limit`` characters (a hunk alone longer than that is split)."""
    out: list[Page] = []
    buf: list[tuple[int | None, int | None, str]] = []
    owners: list[int] = []  # hunk index of each buffered line
    size = 0
    for i, hunk in enumerate(fp.hunks):
        for old, new, text in [(None, None, hunk.header), *hunk.numbered()]:
            # escaped cost: < > become 4 characters, & becomes 5 (the classic page is escaped)
            cost = len(text) + 1 + 3 * (text.count("<") + text.count(">")) + 4 * text.count("&")
            if buf and size + cost > limit:
                out.append(Page("\n".join(t for _, _, t in buf), tuple(buf), owners[0], owners[-1]))
                buf, owners, size = [], [], 0
            buf.append((old, new, text[: limit - 1]))
            owners.append(i)
            size += cost
    if buf:
        out.append(Page("\n".join(t for _, _, t in buf), tuple(buf), owners[0], owners[-1]))
    return out or [Page("(no textual changes)", (), 0, 0)]


def locate(page_lines: tuple[tuple[int | None, int | None, str], ...], quote: str
           ) -> list[tuple[str, int, int]]:  # fmt: skip
    """Every match of ``quote`` in the shown lines: (side, start, end). New side for + and context lines;
    old side for a span of only - lines (§14.11.5)."""
    wanted = [q.strip() for q in quote.strip("\n").splitlines() if q.strip()]
    if not wanted:
        return []
    texts = [t[1:].strip() if t[:1] in {"+", "-", " "} else t.strip() for _, _, t in page_lines]
    hits: list[tuple[str, int, int]] = []
    for i in range(len(texts) - len(wanted) + 1):
        if all(wanted[j] in texts[i + j] for j in range(len(wanted))):
            span = page_lines[i : i + len(wanted)]
            news = [n for _, n, t in span if n is not None]
            olds = [o for o, _, t in span if o is not None]
            if all(t.startswith("-") for _, _, t in span) and olds:
                hits.append(("old", min(olds), max(olds)))
            elif news:
                hits.append(("new", min(news), max(news)))
    return hits
