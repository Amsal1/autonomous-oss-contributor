#!/usr/bin/env python3
"""``git diff --numstat`` (stdin) -> JSON (stdout). Stdlib only; baked into the sandbox images.

Output: ``[{"added": int|null, "deleted": int|null, "path": str}, ...]`` in input order; ``null``
marks a binary file (``-``). The gatekeeper compares this set with its own NUMSTAT derivation
(DESIGN §9.11 step 3, §12.5 step 6); it is never displayed. Exit 2 on an unparseable line.
"""

from __future__ import annotations

import json
import re
import sys

LINE = re.compile(r"^(-|\d+)\t(-|\d+)\t(.+)$")
MAX_INPUT = 64 * 1024 * 1024


def convert(text: str) -> list[dict[str, int | str | None]]:
    out: list[dict[str, int | str | None]] = []
    for line in text.splitlines():
        if not line:
            continue
        match = LINE.match(line)
        if match is None:
            raise ValueError(f"unparseable numstat line: {line[:200]!r}")
        added, deleted, path = match.groups()
        out.append(
            {
                "added": None if added == "-" else int(added),
                "deleted": None if deleted == "-" else int(deleted),
                "path": path,
            }
        )
    return out


def main() -> int:
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        print("numstat2json: input too large", file=sys.stderr)
        return 2
    try:
        entries = convert(data.decode("utf-8", "surrogateescape"))
    except ValueError as exc:
        print(f"numstat2json: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(entries, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
