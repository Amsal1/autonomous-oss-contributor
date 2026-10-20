"""Personal-value, secret and host-path check for the demo capture and pages (dev-only).

    personal_check.py FILE...  [--root DIR] [--profile FILE]

Runs before anything is rendered (on ``transcript.json``) and again on the generated HTML, so the PNG and
GIF outputs (binary, which ``ossctl lint personal`` skips) are made only from text that passed:

* every value of the owner's local ``config/profile.yaml`` (personal and strict, applied to every file),
  through ``ossbot.ctl.lint_personal.scan`` (hits print ``path:line: key``, never a value);
* the secret patterns of ``ossbot.common.redact``;
* host paths that would reveal the machine (``/tmp/``, ``/home/``, a Windows user directory, ...).

Exit 0 when clean, 1 on any hit. Without a profile the profile part has nothing to compare and passes.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from ossbot.common.redact import DEFAULT_SECRET_PATTERNS
from ossbot.common.yamlio import safe_load
from ossbot.ctl import lint_personal

ROOT = Path(__file__).resolve().parents[3]
# needles searched for in the text, never used as paths
HOST_PATHS = (
    "/tmp/",  # noqa: S108
    "pytest-of-", "/home/", "/mnt/", "\\Users\\", "\\\\Users\\\\", "D:\\", "D:/", "C:\\", "/srv/ossbot",
    "/var/lib/ossbot", "/etc/ossbot",
)  # fmt: skip


def profile_values(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    data = safe_load(path.read_bytes()) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path.name} is not a mapping")
    return lint_personal.personal_values(data) + lint_personal.strict_values(data)


def check(files: list[Path], *, root: Path, profile: Path) -> list[str]:
    """Hit lines (``path:line: reason``), never the matched value."""
    hits: list[str] = []
    rels = []
    for f in files:
        resolved = f.resolve()
        try:
            rels.append(resolved.relative_to(root.resolve()).as_posix())
        except ValueError:
            raise SystemExit(f"{f} is outside {root}") from None
    values = profile_values(profile)
    if values:
        for hit in lint_personal.scan(root, rels, values, strict=values):
            hits.append(f"{hit.path}:{hit.line}: value of profile key {hit.key}")
    secrets = [re.compile(p) for p in DEFAULT_SECRET_PATTERNS]
    for rel in rels:
        for lineno, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), start=1):
            hits.extend(f"{rel}:{lineno}: secret pattern {i}" for i, rx in enumerate(secrets) if rx.search(line))
            hits.extend(f"{rel}:{lineno}: host path {needle!r}" for needle in HOST_PATHS if needle in line)
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--profile", type=Path, default=None)
    args = parser.parse_args(argv)
    profile = args.profile or args.root / "config" / "profile.yaml"
    files: list[Path] = []
    for f in args.files:
        files.extend(sorted(f.glob("*.html")) if f.is_dir() else [f])
    hits = check(files, root=args.root, profile=profile)
    for line in hits:
        print(line)
    state = "present" if profile.is_file() else "absent"
    print(f"personal_check: {len(files)} file(s), profile {state}: {len(hits)} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
