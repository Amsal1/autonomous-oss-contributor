"""The S6 interdiff, computed by the gatekeeper at ``/package`` of rev N (DESIGN §14.11.7, ADR-0019; AC 9).

* Same ``base_sha`` as rev N-1: ``INTERDIFF old_head new_head`` → ``interdiff.patch`` (a real patch:
  per-file counts and new-side line ranges, so "touched" uses the ±3-line window).
* Moved base: ``RANGE_DIFF old_base..old_head new_base..new_head`` → ``interdiff.patch`` (range-diff text,
  not a patch). ``files[]`` comes from comparing each path's normalized hunk text in the two derived
  ``FORMAT_PATCH`` outputs (``index`` lines removed, every ``@@ … @@`` header replaced by ``@@``);
  "touched" is file-level.
* Both run in the rev N mirror with a temporary ``objects/info/alternates`` line for the rev N-1
  mirror, removed right after. A failure writes ``{"mode": "unavailable", "reason": …}``; the card is
  still sent with "Interdiff unavailable".

Display artifact only (U4): nothing here is bound.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ossbot.common.gitcmd import INTERDIFF, RANGE_DIFF
from ossbot.gatekeeper.gitops import GitError, GitOps
from ossbot.gatekeeper.telegram.review import patchview

log = logging.getLogger(__name__)
_HUNK_HEADER = re.compile(r"^@@ .*? @@")


@contextmanager
def _alternate(mirror: Path, other: Path) -> Iterator[None]:
    info = mirror / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    path = info / "alternates"
    before = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text((before or "") + f"{(other / 'objects').resolve()}\n", encoding="utf-8")
    try:
        yield
    finally:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(before, encoding="utf-8")


def _normalized(fp: patchview.FilePatch) -> list[str]:
    out = []
    for h in fp.hunks:
        out.append("@@")
        out.extend(h.lines)
    return out


def range_files(old_patch: str, new_patch: str) -> list[dict[str, Any]]:
    """Paths whose normalized hunk text differs between the two FORMAT_PATCH outputs."""
    old = {f.path: _normalized(f) for f in patchview.parse(old_patch)}
    new = {f.path: _normalized(f) for f in patchview.parse(new_patch)}
    files: list[dict[str, Any]] = []
    for path in sorted(set(old) | set(new)):
        a, b = old.get(path), new.get(path)
        if a == b:
            continue
        a_set, b_set = set(a or []), set(b or [])
        files.append({"path": path, "added": sum(1 for x in (b or []) if x not in a_set and x != "@@"),
                      "deleted": sum(1 for x in (a or []) if x not in b_set and x != "@@")})  # fmt: skip
    return files


def compute(
    ops: GitOps, *, upstream: str, cid: str, rev: int, old: dict[str, Any], new: dict[str, Any]
) -> dict[str, Any]:
    """``old``/``new``: the ``meta.json`` of the two revs' derived dirs (``round_base`` = the base, ``head``)."""
    out_dir = ops.derived_dir(cid, rev)
    old_mirror = ops.mirror_dir(upstream, cid, rev - 1)
    new_mirror = ops.mirror_dir(upstream, cid, rev)
    info: dict[str, Any]
    try:
        if not old_mirror.exists():
            raise FileNotFoundError("previous mirror gone")
        same_base = bool(old.get("round_base")) and old.get("round_base") == new.get("round_base")
        with _alternate(new_mirror, old_mirror):
            if same_base:
                text = ops.git.run([*INTERDIFF, str(old["head"]), str(new["head"])], cwd=new_mirror).stdout
                patch = text.decode("utf-8", "replace")
                files = [{"path": f.path, "added": f.added, "deleted": f.deleted,
                          "new_ranges": [list(h.new_range()) for h in f.hunks]}
                         for f in patchview.parse(patch)]  # fmt: skip
                info = {"mode": "interdiff", "base_changed": False, "touch_granularity": "line", "files": files}
            else:
                ranges = [f"{old['round_base']}..{old['head']}", f"{new['round_base']}..{new['head']}"]
                text = ops.git.run([*RANGE_DIFF, *ranges], cwd=new_mirror).stdout
                old_patch = (ops.derived_dir(cid, rev - 1) / "change.patch").read_bytes().decode("utf-8", "replace")
                new_patch = (out_dir / "change.patch").read_bytes().decode("utf-8", "replace")
                info = {"mode": "range_diff", "base_changed": True, "touch_granularity": "file",
                        "old_base": old.get("round_base"), "new_base": new.get("round_base"),
                        "files": range_files(old_patch, new_patch)}  # fmt: skip
        (out_dir / "interdiff.patch").write_bytes(text)
    except (OSError, GitError, KeyError) as exc:
        log.warning("interdiff unavailable for %s r%d: %s", cid, rev, type(exc).__name__,
                    extra={"event": "interdiff_unavailable"})  # fmt: skip
        info = {"mode": "unavailable", "reason": "previous revision gone" if isinstance(exc, FileNotFoundError)
                else type(exc).__name__}  # fmt: skip
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "interdiff.json").write_text(json.dumps(info, sort_keys=True), encoding="utf-8")
    return info


def write_unavailable(ops: GitOps, cid: str, rev: int, reason: str) -> dict[str, Any]:
    info = {"mode": "unavailable", "reason": reason}
    out = ops.derived_dir(cid, rev)
    out.mkdir(parents=True, exist_ok=True)
    (out / "interdiff.json").write_text(json.dumps(info, sort_keys=True), encoding="utf-8")
    return info


def read_meta(ops: GitOps, cid: str, rev: int) -> dict[str, Any] | None:
    try:
        doc = json.loads((ops.derived_dir(cid, rev) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None
