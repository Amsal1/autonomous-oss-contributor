"""``ossctl lint personal``: no owner value from ``config/profile.yaml`` in a tracked file outside ``docs/``,
and no strict value (``strict_values``) in any tracked file at all.

DESIGN NFR-11, AC 23. The owner's values live only in the gitignored ``config/profile.yaml``; this
check reads them, lists the tracked files (``git ls-files``, which includes newly staged files, so the
pre-commit hook covers them) and reports ``path:line: key`` for every hit. It never prints a value.
Without a profile (CI, a fresh clone) there is nothing to compare against and it passes; CI checks
``config/profile.example.yaml`` against the placeholder pattern instead (test_profile_example.py).

Two tiers, both read at runtime from the gitignored profile only:
- personal values (``PERSONAL_KEYS``) are allowed in ``docs/`` and in the attribution files
  (``ATTRIBUTION_FILES``: NOTICE, the community files), which credit the author by name and GitHub login;
- strict values (``strict_values``: the identities the owner must never use) are refused everywhere,
  ``docs/`` and the attribution files included.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterator

from ossbot.common.yamlio import YamlError, safe_load

PROFILE = Path("config/profile.yaml")
EXEMPT_PREFIXES = ("docs/",)  # personal values are allowed in the design and plan documents
# the author credit (name, GitHub login, repo URL) of the license and community files; never strict values
ATTRIBUTION_FILES = frozenset({
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    ".github/ISSUE_TEMPLATE/config.yml",
})  # fmt: skip
MIN_LEN = 4  # shorter values ("me", "AK") would match everywhere and carry no identity
MAX_FILE_BYTES = 2_000_000
# Keys whose values identify the owner. Generic settings (region, time zone, quiet hours, interests
# categories, public prior contributions) are not personal and stay out.
PERSONAL_KEYS: tuple[tuple[str, ...], ...] = (
    ("github_login",),
    ("commit_name",),
    ("commit_email",),
    ("forbidden_logins",),
    ("confidentiality_floor",),
    ("telegram", "user_id"),
    ("kiro", "idc_start_url"),
    ("backup", "age_recipients"),
    ("review_mirror", "repo"),
    ("interests", "summary"),
)


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    key: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: value of profile key {self.key}"


class LintError(RuntimeError):
    pass


def _lookup(data: Any, key: tuple[str, ...]) -> Any:
    for part in key:
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def personal_values(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """``(key, value)`` pairs worth searching for; empty, zero and too-short values are skipped."""
    out: list[tuple[str, str]] = []
    for key in PERSONAL_KEYS:
        raw = _lookup(profile, key)
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            if value is None or isinstance(value, bool) or value == 0:
                continue
            text = str(value).strip()
            if len(text) >= MIN_LEN:
                out.append((".".join(key), text))
    return out


def strict_values(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """The strict values, refused in every tracked file (``docs/`` included): each ``forbidden_logins``
    entry, the domain of each ``@domain`` entry of ``confidentiality_floor`` (so ``x@domain`` and
    ``sub.domain`` hit too), and the host of ``kiro.idc_start_url`` on its own."""
    out: list[tuple[str, str]] = []
    logins = profile.get("forbidden_logins")
    for value in logins if isinstance(logins, list) else []:
        out.append(("forbidden_logins", str(value).strip()))
    floor = profile.get("confidentiality_floor")
    for value in floor if isinstance(floor, list) else []:
        text = str(value).strip()
        if text.startswith("@"):
            out.append(("confidentiality_floor", text[1:]))
    url = _lookup(profile, ("kiro", "idc_start_url"))
    if isinstance(url, str) and url.strip():
        host = urlsplit(url.strip()).hostname
        if host:
            out.append(("kiro.idc_start_url (host)", host))
    return [(key, value) for key, value in out if len(value) >= MIN_LEN]


def _pattern(value: str) -> re.Pattern[str]:
    # no letter/digit glued on either side, so "alice" does not hit "malice" but hits "@alice" or "alice/x"
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", re.IGNORECASE)


def tracked_files(root: Path) -> list[str]:
    git = shutil.which("git") or "/usr/bin/git"
    proc = subprocess.run([git, "-C", str(root), "ls-files", "-z"], capture_output=True, check=False, timeout=60)
    if proc.returncode != 0:
        raise LintError(f"git ls-files failed: {proc.stderr.decode(errors='replace').strip()}")
    return [p for p in proc.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _worktree_blobs(root: Path, files: list[str]) -> Iterator[tuple[str, bytes]]:
    for rel in files:
        path = root / rel
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                continue
            yield rel, path.read_bytes()
        except OSError:
            continue


def _staged_blobs(root: Path, files: list[str]) -> Iterator[tuple[str, bytes]]:
    """The index content of each file (what the commit will contain), via one ``git cat-file --batch``."""
    wanted = [rel for rel in files if "\n" not in rel]
    if not wanted:
        return
    git = shutil.which("git") or "/usr/bin/git"
    request = "".join(f":{rel}\n" for rel in wanted).encode("utf-8")
    proc = subprocess.run(
        [git, "-C", str(root), "cat-file", "--batch"], input=request, capture_output=True, check=False, timeout=120
    )
    if proc.returncode != 0:
        raise LintError(f"git cat-file failed: {proc.stderr.decode(errors='replace').strip()}")
    out, pos = proc.stdout, 0
    for rel in wanted:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        pos = end + 1
        if len(header) != 3:  # "<name> missing" (e.g. a gitlink); nothing to read
            continue
        size = int(header[2])
        blob = out[pos : pos + size]
        pos += size + 1
        if header[1] == b"blob" and size <= MAX_FILE_BYTES:
            yield rel, blob


def _exempt(rel: str) -> bool:
    return rel.startswith(EXEMPT_PREFIXES) or rel in ATTRIBUTION_FILES


def scan(
    root: Path,
    files: list[str],
    values: list[tuple[str, str]],
    *,
    staged: bool = False,
    strict: list[tuple[str, str]] | None = None,
) -> list[Hit]:
    """``values`` are searched outside the exempt paths, ``strict`` values in every file. One hit per
    path, line and key."""
    patterns = [(key, _pattern(value)) for key, value in values]
    strict_patterns = [(key, _pattern(value)) for key, value in strict or []]
    if not strict_patterns:
        files = [rel for rel in files if not _exempt(rel)]
    hits: list[Hit] = []
    blobs = _staged_blobs(root, files) if staged else _worktree_blobs(root, files)
    for rel, blob in blobs:
        if b"\0" in blob[:8192]:  # binary
            continue
        active = strict_patterns if _exempt(rel) else patterns + strict_patterns
        for lineno, line in enumerate(blob.decode("utf-8", errors="replace").splitlines(), start=1):
            seen: set[str] = set()
            for key, pattern in active:
                if key not in seen and pattern.search(line):
                    seen.add(key)
                    hits.append(Hit(rel, lineno, key))
    return hits


def run(root: Path, *, staged: bool = False) -> tuple[list[Hit], str]:
    """Returns the hits and a one-line summary. Raises LintError when the profile itself is tracked.

    ``staged`` scans the index blobs instead of the working tree, so a partially staged file is checked
    exactly as it will be committed (the pre-commit hook passes ``--staged``)."""
    files = tracked_files(root)
    if PROFILE.as_posix() in files:
        raise LintError(f"{PROFILE.as_posix()} is tracked; it must stay gitignored (git rm --cached it)")
    profile_path = root / PROFILE
    if not profile_path.is_file():
        return [], f"no {PROFILE.as_posix()}: nothing to compare (CI checks config/profile.example.yaml)"
    try:
        profile = safe_load(profile_path.read_bytes()) or {}
    except (YamlError, UnicodeDecodeError) as exc:
        raise LintError(f"{PROFILE.as_posix()} is not valid YAML: {exc}") from exc
    if not isinstance(profile, dict):
        raise LintError(f"{PROFILE.as_posix()} is not a mapping")
    values = personal_values(profile)
    strict = strict_values(profile)
    hits = scan(root, files, values, staged=staged, strict=strict)
    kind = "staged" if staged else "tracked"
    summary = (
        f"checked {len(files)} {kind} files for {len(values)} profile values outside docs/ and the attribution"
        f" files, and {len(strict)} strict values everywhere: {len(hits)} hit(s)"
    )
    return hits, summary
