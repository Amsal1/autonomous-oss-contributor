"""The root half of the skill gate (DESIGN §15.5; ADR-0002 and ADR-0018 addenda FEAT-007).

Hermes v0.22.2 with ``skills.write_approval: true`` stages every ``skill_manage`` mutation as a JSON
record ``~hermes/.hermes/pending/skills/<id>.json`` holding the replay payload; nothing reaches a
skill root, and the active dir ``~hermes/.hermes/skills`` is ``root:hermes`` 0750/0640 anyway.

* ``scan`` (root timer): reads each record as untrusted data (``O_NOFOLLOW``, size and count limits,
  strict shapes), applies its operations to a copy of the active skill, stores the resulting bytes in
  root-only ``/var/lib/ossbot/skills/staged/<review>/`` and lists every review with its file hashes,
  declared tools, tier and an escaped-on-render diff in ``pending.json`` (root:gatekeeper 0640). The
  gatekeeper imports that into ``skill_reviews`` and sends the two-tap skill card.
* ``apply`` (root path unit on ``control/skill-request-*.json``, written by the gatekeeper after the
  Confirm and ``consume``): re-hashes the stored bytes, requires the digest the approval bound, writes
  exactly those bytes into the active dir through directory file descriptors (no path is ever resolved
  through a link), removes the pending record, and appends the outcome to ``decisions.json``.
* ``drift``: the active tree against the last activated digests (doctor).

Root never imports, runs or acts on a hermes-owned file as code or config: records are parsed data.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import errno
import grp
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERMES_DIR = Path("/home/hermes/.hermes")
PENDING_DIR = HERMES_DIR / "pending" / "skills"
ACTIVE_DIR = HERMES_DIR / "skills"
STORE = Path("/var/lib/ossbot/skills")
CONTROL_DIR = Path("/var/lib/ossbot/control")
REQUEST_RE = re.compile(r"^skill-request-[A-Za-z0-9_-]{1,64}\.json$")
RECORD_RE = re.compile(r"^[0-9a-f]{6,32}\.json$")
REVIEW_RE = re.compile(r"^r_[0-9a-f]{20}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
MAX_RECORD = 1024 * 1024
MAX_RECORDS = 50
MAX_FILES = 50
MAX_FILE = 256 * 1024
MAX_TREE = 1024 * 1024
MAX_DIFF = 64 * 1024
MAX_DEPTH = 4
DECISIONS_KEEP = 500
SKILL_MD = "SKILL.md"
ACTIONS = ("create", "edit", "patch", "delete", "write_file", "remove_file")


class StagingError(ValueError):
    """A staged record that cannot become a review (shape, size, path, conflict)."""


# ------------------------------------------------------------------------ fd-safe file access
def open_dir(path: Path) -> int:
    """Open a directory walking every component with ``O_NOFOLLOW``: no link anywhere on the path."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read(dir_fd: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StagingError(f"{name} is not a regular file")
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        if len(data) > limit:
            raise StagingError(f"{name} is larger than {limit} bytes")
        return data
    finally:
        os.close(fd)


def read_tree(dir_fd: int, prefix: str = "", depth: int = 0) -> dict[str, bytes]:
    """Every regular file below ``dir_fd``; a link, special file or limit breach raises."""
    out: dict[str, bytes] = {}
    for name in sorted(os.listdir(dir_fd)):
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        rel = f"{prefix}{name}"
        if stat.S_ISLNK(st.st_mode):
            raise StagingError(f"{rel} is a link")
        if stat.S_ISDIR(st.st_mode):
            if depth >= MAX_DEPTH:
                raise StagingError(f"{rel}: too deep")
            sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
            try:
                out.update(read_tree(sub, rel + "/", depth + 1))
            finally:
                os.close(sub)
        elif stat.S_ISREG(st.st_mode):
            out[rel] = _read(dir_fd, name, MAX_FILE)
        else:
            raise StagingError(f"{rel} is a special file")
        if len(out) > MAX_FILES:
            raise StagingError("too many files")
    return out


def digest(files: Mapping[str, bytes]) -> str:
    lines = [f"{p}\0{hashlib.sha256(files[p]).hexdigest()}" for p in sorted(files)]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def file_hashes(files: Mapping[str, bytes]) -> dict[str, str]:
    return {p: hashlib.sha256(files[p]).hexdigest() for p in sorted(files)}


# ------------------------------------------------------------------------- the operations
def _rel_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise StagingError("bad file path")
    parts = value.split("/")
    if len(parts) > MAX_DEPTH or any(not COMPONENT_RE.match(p) for p in parts):
        raise StagingError(f"file path not allowed: {value[:80]!r}")
    return value


def _text(value: Any, field: str) -> bytes:
    if not isinstance(value, str):
        raise StagingError(f"{field} must be a string")
    data = value.encode("utf-8")
    if len(data) > MAX_FILE:
        raise StagingError(f"{field} is larger than {MAX_FILE} bytes")
    return data


def _skill_name(value: Any) -> tuple[str | None, str]:
    """``name`` or ``category/name`` -> (category, name)."""
    if not isinstance(value, str):
        raise StagingError("skill name missing")
    cat, _, name = value.rpartition("/")
    if not NAME_RE.match(name) or (cat and not NAME_RE.match(cat)) or "/" in cat:
        raise StagingError(f"skill name not allowed: {value[:80]!r}")
    return cat or None, name


def apply_ops(base: dict[str, bytes] | None, payload: Mapping[str, Any]) -> tuple[dict[str, bytes] | None, str | None]:
    """The tree after the record's operations (``None`` = delete) and the category of a create."""
    ops = payload.get("operations")
    steps = ops if isinstance(ops, list) else [payload]
    if not steps or len(steps) > 20:
        raise StagingError("no or too many operations")
    files = dict(base) if base is not None else None
    category: str | None = None
    for step in steps:
        if not isinstance(step, dict):
            raise StagingError("operation is not an object")
        action = step.get("action")
        if action not in ACTIONS:
            raise StagingError(f"unsupported action {str(action)[:40]!r}")
        if action == "create":
            if files is not None:
                raise StagingError("create on an existing skill")
            category = step.get("category") if isinstance(step.get("category"), str) else None
            if category is not None and not NAME_RE.match(category):
                raise StagingError("bad category")
            files = {SKILL_MD: _text(step.get("content"), "content")}
            continue
        if files is None:
            raise StagingError(f"{action} on a skill that does not exist")
        if action == "delete":
            files = None
        elif action == "edit" or (action == "patch" and step.get("content") and step.get("old_string") is None):
            files[SKILL_MD] = _text(step.get("content"), "content")
        elif action == "patch":
            path = _rel_path(step.get("file_path") or SKILL_MD)
            if path not in files:
                raise StagingError(f"patch of a missing file {path}")
            old, new = _text(step.get("old_string"), "old_string"), _text(step.get("new_string"), "new_string")
            if not old or old not in files[path]:
                raise StagingError("old_string not found")
            files[path] = files[path].replace(old, new) if step.get("replace_all") else files[path].replace(old, new, 1)
        elif action == "write_file":
            files[_rel_path(step.get("file_path"))] = _text(step.get("file_content"), "file_content")
        elif action == "remove_file":
            path = _rel_path(step.get("file_path"))
            if path == SKILL_MD or path not in files:
                raise StagingError(f"cannot remove {path}")
            del files[path]
    if files is not None:
        if SKILL_MD not in files:
            raise StagingError("a skill needs SKILL.md")
        if len(files) > MAX_FILES or sum(len(v) for v in files.values()) > MAX_TREE:
            raise StagingError("skill tree over the size limits")
    return files, category


def declared_tools(skill_md: bytes) -> list[str]:
    """Tools a SKILL.md frontmatter declares (``allowed-tools``/``tools``); shown on the card, checked
    against the tier. The rendered config, not a skill, grants tools."""
    text = skill_md.decode("utf-8", "replace")
    if not text.startswith("---"):
        return []
    front = text[3:].split("\n---", 1)[0]
    out: list[str] = []
    key: str | None = None
    for line in front.splitlines():
        m = re.match(r"^(allowed-tools|allowed_tools|tools)\s*:\s*(.*)$", line.strip())
        if m:
            key = m.group(1)
            value = m.group(2).strip()
            if value.startswith("["):
                out += [t.strip().strip("'\"") for t in value.strip("[]").split(",") if t.strip()]
                key = None
            elif value:
                out += [t.strip().strip("'\"") for t in value.split(",") if t.strip()]
                key = None
        elif key and re.match(r"^\s*-\s+", line):
            out.append(line.split("-", 1)[1].strip().strip("'\""))
        elif line and not line.startswith((" ", "\t")):
            key = None
    return sorted({t[:64] for t in out if t})[:50]


def make_diff(old: Mapping[str, bytes] | None, new: Mapping[str, bytes] | None) -> str:
    chunks: list[str] = []
    for path in sorted(set(old or {}) | set(new or {})):
        a = (old or {}).get(path, b"").decode("utf-8", "replace").splitlines(keepends=True)
        b = (new or {}).get(path, b"").decode("utf-8", "replace").splitlines(keepends=True)
        if a != b:
            chunks += difflib.unified_diff(a, b, f"a/{path}", f"b/{path}")
    text = "".join(chunks)
    return text if len(text) <= MAX_DIFF else text[:MAX_DIFF] + "\n… (diff truncated)\n"


# --------------------------------------------------------------------------- active skills
def find_active(skills_fd: int, name: str) -> tuple[str | None, dict[str, bytes] | None]:
    """``(rel_dir, files)`` of the active skill ``name`` (``<name>`` or ``<category>/<name>``)."""
    found: list[str] = []
    for entry in sorted(os.listdir(skills_fd)):
        if entry.startswith("."):
            continue
        st = os.stat(entry, dir_fd=skills_fd, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            continue
        if entry == name:
            found.append(entry)
            continue
        sub = os.open(entry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=skills_fd)
        try:
            if name in os.listdir(sub):
                found.append(f"{entry}/{name}")
        finally:
            os.close(sub)
    if not found:
        return None, None
    if len(found) > 1:
        raise StagingError(f"ambiguous skill name {name}")
    rel = found[0]
    fd = _open_rel(skills_fd, rel)
    try:
        return rel, read_tree(fd)
    finally:
        os.close(fd)


def _open_rel(base_fd: int, rel: str) -> int:
    fd = os.dup(base_fd)
    for part in rel.split("/"):
        nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        os.close(fd)
        fd = nxt
    return fd


# --------------------------------------------------------------------------------- staging
@dataclass
class Review:
    review_id: str
    pending_id: str
    skill: str
    rel_dir: str
    action: str
    files: dict[str, str]
    content_sha256: str
    declared_tools: list[str]
    tier: str
    summary: str
    origin: str
    created_at: str
    diff: str

    def manifest(self) -> dict[str, Any]:
        return dict(self.__dict__)


def stage_record(raw: bytes, pending_id: str, skills_fd: int, tier: str) -> tuple[Review, dict[str, bytes] | None]:
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise StagingError("record is not JSON") from exc
    if (
        not isinstance(record, dict)
        or record.get("subsystem") != "skills"
        or not isinstance(record.get("payload"), dict)
    ):
        raise StagingError("not a skills record")
    payload = record["payload"]
    category, name = _skill_name(payload.get("name"))
    rel_dir, base = find_active(skills_fd, name)
    files, created_cat = apply_ops(base, payload)
    if rel_dir is None:
        cat = created_cat or category
        rel_dir = f"{cat}/{name}" if cat else name
    action = "delete" if files is None else ("create" if base is None else "update")
    content = digest(files or {})
    review_id = "r_" + hashlib.sha256(f"{pending_id}\0{content}\0{action}".encode()).hexdigest()[:20]
    created = record.get("created_at")
    review = Review(
        review_id=review_id,
        pending_id=pending_id,
        skill=name,
        rel_dir=rel_dir,
        action=action,
        files=file_hashes(files or {}),
        content_sha256=content,
        declared_tools=declared_tools((files or {}).get(SKILL_MD, b"")),
        tier=tier,
        summary=str(record.get("summary") or "")[:300],
        origin=str(record.get("origin") or "foreground")[:40],
        created_at=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(created if isinstance(created, (int, float)) else time.time())
        ),
        diff=make_diff(base, files),
    )
    return review, files


def _write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _write_shared(path: Path, doc: Any, group: str = "gatekeeper") -> None:
    """root:<group> 0640 JSON, atomically (what the gatekeeper reads)."""
    data = (json.dumps(doc, sort_keys=True, indent=1) + "\n").encode()
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    with contextlib.suppress(KeyError, PermissionError):
        os.chown(tmp, 0, grp.getgrnam(group).gr_gid)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def _store_dirs(store: Path) -> Path:
    store.mkdir(mode=0o750, exist_ok=True)
    with contextlib.suppress(KeyError, PermissionError):
        os.chown(store, 0, grp.getgrnam("gatekeeper").gr_gid)
    staged = store / "staged"
    staged.mkdir(mode=0o700, exist_ok=True)
    return staged


def _pending_records(pending_dir: Path) -> Iterator[tuple[str, bytes]]:
    try:
        fd = open_dir(pending_dir)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise StagingError(f"{pending_dir} is not a plain directory") from exc
        raise
    try:
        names = sorted(n for n in os.listdir(fd) if RECORD_RE.match(n))[:MAX_RECORDS]
        for name in names:
            try:
                yield name[: -len(".json")], _read(fd, name, MAX_RECORD)
            except (StagingError, OSError):
                yield name[: -len(".json")], b""
    finally:
        os.close(fd)


def scan(
    tier: str, *, pending_dir: Path = PENDING_DIR, active_dir: Path = ACTIVE_DIR, store: Path = STORE
) -> dict[str, Any]:
    """Stage every pending record; write ``pending.json``; drop staged dirs no record backs any more."""
    staged_root = _store_dirs(store)
    skills_fd = open_dir(active_dir)
    reviews: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    try:
        for pending_id, raw in _pending_records(pending_dir):
            try:
                review, files = stage_record(raw, pending_id, skills_fd, tier)
            except (StagingError, OSError) as exc:
                invalid.append({"pending_id": pending_id, "reason": str(exc)[:200]})
                continue
            target = staged_root / review.review_id
            if not target.is_dir():
                tmp = staged_root / f".{review.review_id}.tmp"
                if tmp.exists():
                    _rmtree(tmp)
                tmp.mkdir(mode=0o700)
                for rel, data in (files or {}).items():
                    (tmp / "files" / rel).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    _write_private(tmp / "files" / rel, data)
                _write_private(tmp / "manifest.json", json.dumps(review.manifest(), sort_keys=True).encode())
                os.rename(tmp, target)
            reviews.append(review.manifest())
    finally:
        os.close(skills_fd)
    keep = {r["review_id"] for r in reviews} | _requested_reviews()
    for entry in os.listdir(staged_root):
        if REVIEW_RE.match(entry) and entry not in keep:
            _rmtree(staged_root / entry)
    doc = {"reviews": reviews, "invalid": invalid, "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _write_shared(store / "pending.json", doc)
    return doc


def _rmtree(path: Path) -> None:
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            os.unlink(os.path.join(root, f))
        for d in dirs:
            p = os.path.join(root, d)
            if os.path.islink(p):
                os.unlink(p)
            else:
                os.rmdir(p)
    os.rmdir(path)


def _requested_reviews(control_dir: Path = CONTROL_DIR) -> set[str]:
    out: set[str] = set()
    with contextlib.suppress(OSError):
        for name in os.listdir(control_dir):
            if REQUEST_RE.match(name):
                with contextlib.suppress(OSError, ValueError, StagingError):
                    fd = open_dir(control_dir)
                    try:
                        doc = json.loads(_read(fd, name, 4096))
                    finally:
                        os.close(fd)
                    if isinstance(doc, dict) and isinstance(doc.get("review_id"), str):
                        out.add(doc["review_id"])
    return out


# ------------------------------------------------------------------------------- activation
def load_staged(review_id: str, store: Path = STORE) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not REVIEW_RE.match(review_id):
        raise StagingError("bad review id")
    base = store / "staged" / review_id
    fd = open_dir(base)
    try:
        manifest = json.loads(_read(fd, "manifest.json", MAX_RECORD))
    finally:
        os.close(fd)
    files: dict[str, bytes] = {}
    if manifest.get("action") != "delete":
        fd = open_dir(base / "files")
        try:
            files = read_tree(fd)
        finally:
            os.close(fd)
    if digest(files) != manifest.get("content_sha256") or file_hashes(files) != manifest.get("files"):
        raise StagingError("staged bytes do not match their manifest")
    return manifest, files


def _write_tree(parent_fd: int, name: str, files: Mapping[str, bytes], uid: int, gid: int) -> None:
    os.mkdir(name, 0o750, dir_fd=parent_fd)
    top = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        os.fchown(top, uid, gid)
        os.fchmod(top, 0o750)
        for rel in sorted(files):
            fd = os.dup(top)
            parts = rel.split("/")
            for part in parts[:-1]:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o750, dir_fd=fd)
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = nxt
                os.fchown(fd, uid, gid)
                os.fchmod(fd, 0o750)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            out = os.open(parts[-1], flags, 0o640, dir_fd=fd)
            with os.fdopen(out, "wb") as fh:
                fh.write(files[rel])
                os.fchown(fh.fileno(), uid, gid)
                os.fchmod(fh.fileno(), 0o640)
            os.close(fd)
    finally:
        os.close(top)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        for entry in os.listdir(fd):
            st = os.stat(entry, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                _remove_tree_at(fd, entry)
            else:
                os.unlink(entry, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def activate(
    manifest: Mapping[str, Any],
    files: Mapping[str, bytes],
    *,
    active_dir: Path = ACTIVE_DIR,
    uid: int = 0,
    gid: int | None = None,
) -> None:
    """Exactly the bound bytes into the root-owned active dir, via directory fds, new tree swapped in
    (``uid``/``gid``: root and group hermes; tests pass their own)."""
    gid = grp.getgrnam("hermes").gr_gid if gid is None else gid
    skills_fd = open_dir(active_dir)
    try:
        st = os.fstat(skills_fd)
        if st.st_uid != uid or st.st_mode & 0o022:
            raise StagingError(f"{active_dir} is not root-owned and read-only for others")
        rel = str(manifest["rel_dir"])
        parts = rel.split("/")
        if len(parts) > 2 or any(not NAME_RE.match(p) for p in parts):
            raise StagingError("bad skill dir")
        parent = os.dup(skills_fd)
        try:
            if len(parts) == 2:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(parts[0], 0o750, dir_fd=parent)
                nxt = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                os.close(parent)
                parent = nxt
                os.fchown(parent, uid, gid)
                os.fchmod(parent, 0o750)
            leaf = parts[-1]
            new, old = f".ossbot-new-{leaf}", f".ossbot-old-{leaf}"
            for stale in (new, old):
                with contextlib.suppress(FileNotFoundError):
                    _remove_tree_at(parent, stale)
            exists = True
            try:
                os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                exists = False
            if manifest.get("action") != "delete":
                _write_tree(parent, new, files, uid, gid)
            if exists:
                os.rename(leaf, old, src_dir_fd=parent, dst_dir_fd=parent)
            if manifest.get("action") != "delete":
                os.rename(new, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            if exists:
                _remove_tree_at(parent, old)
        finally:
            os.close(parent)
    finally:
        os.close(skills_fd)


def _unlink_pending(pending_id: str, pending_dir: Path = PENDING_DIR) -> None:
    if not re.match(r"^[0-9a-f]{6,32}$", pending_id):
        return
    with contextlib.suppress(OSError):
        fd = open_dir(pending_dir)
        try:
            os.unlink(f"{pending_id}.json", dir_fd=fd)
        finally:
            os.close(fd)


def _append_decision(entry: dict[str, Any], store: Path = STORE) -> None:
    path = store / "decisions.json"
    try:
        doc = json.loads(path.read_bytes()) if path.is_file() and not path.is_symlink() else {"decisions": []}
    except ValueError:
        doc = {"decisions": []}
    items = [d for d in doc.get("decisions", []) if isinstance(d, dict)][-DECISIONS_KEEP:]
    items.append(entry)
    _write_shared(path, {"decisions": items})


def apply_requests(
    *,
    control_dir: Path = CONTROL_DIR,
    store: Path = STORE,
    pending_dir: Path = PENDING_DIR,
    active_dir: Path = ACTIVE_DIR,
    gatekeeper_uid: int | None = None,
    uid: int = 0,
    gid: int | None = None,
) -> list[dict[str, Any]]:
    """Process every ``skill-request-*.json`` the gatekeeper wrote (owner checked), oldest first."""
    _store_dirs(store)
    done: list[dict[str, Any]] = []
    cfd = open_dir(control_dir)
    try:
        names = sorted(n for n in os.listdir(cfd) if REQUEST_RE.match(n))
        for name in names:
            entry: dict[str, Any] = {"request": name, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            try:
                st = os.stat(name, dir_fd=cfd, follow_symlinks=False)
                if gatekeeper_uid is not None and st.st_uid not in (0, gatekeeper_uid):
                    raise StagingError("request not written by the gatekeeper")
                req = json.loads(_read(cfd, name, 4096))
                if not isinstance(req, dict) or req.get("op") not in ("activate", "reject"):
                    raise StagingError("bad request")
                review_id = str(req.get("review_id"))
                entry.update(op=req["op"], review_id=review_id, approval_id=str(req.get("approval_id"))[:20])
                manifest, files = load_staged(review_id, store)
                entry.update(skill=manifest["skill"], rel_dir=manifest["rel_dir"], action=manifest["action"])
                if req["op"] == "activate":
                    if req.get("content_sha256") != manifest["content_sha256"]:
                        raise StagingError("approved digest differs from the staged bytes")
                    activate(manifest, files, active_dir=active_dir, uid=uid, gid=gid)
                    entry.update(content_sha256=manifest["content_sha256"], files=manifest["files"], result="activated")
                else:
                    entry["result"] = "rejected"
                _unlink_pending(str(manifest["pending_id"]), pending_dir)
                _rmtree(store / "staged" / review_id)
            except (StagingError, OSError, ValueError, KeyError) as exc:
                entry.update(result="failed", reason=str(exc)[:200])
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=cfd)
            _append_decision(entry, store)
            done.append(entry)
    finally:
        os.close(cfd)
    return done


# ------------------------------------------------------------------------------------ drift
def drift(*, active_dir: Path = ACTIVE_DIR, store: Path = STORE, uid: int = 0) -> list[str]:
    """Skills whose active bytes differ from the last activation, and skill dirs nobody activated."""
    try:
        doc = json.loads((store / "decisions.json").read_bytes())
    except (OSError, ValueError):
        doc = {"decisions": []}
    expected: dict[str, str | None] = {}
    for d in doc.get("decisions", []):
        if isinstance(d, dict) and d.get("result") == "activated":
            expected[str(d["rel_dir"])] = None if d.get("action") == "delete" else str(d.get("content_sha256"))
    problems: list[str] = []
    fd = open_dir(active_dir)
    try:
        st = os.fstat(fd)
        if st.st_uid != uid or st.st_mode & 0o022:
            problems.append(f"{active_dir} is not root-owned and read-only")
        present: dict[str, str] = {}
        for top in sorted(os.listdir(fd)):
            if top.startswith(".ossbot-"):
                continue
            tst = os.stat(top, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(tst.st_mode):
                problems.append(f"unexpected entry {top}")
                continue
            sub = _open_rel(fd, top)
            try:
                names = os.listdir(sub)
                if SKILL_MD in names:
                    present[top] = digest(read_tree(sub))
                else:
                    for leaf in sorted(names):
                        leaf_fd = _open_rel(sub, leaf)
                        try:
                            present[f"{top}/{leaf}"] = digest(read_tree(leaf_fd))
                        finally:
                            os.close(leaf_fd)
            except (StagingError, OSError) as exc:
                problems.append(f"{top}: {exc}")
            finally:
                os.close(sub)
    finally:
        os.close(fd)
    for rel, have in present.items():
        if rel not in expected or expected[rel] is None:
            problems.append(f"{rel}: active without an approval")
        elif expected[rel] != have:
            problems.append(f"{rel}: differs from the approved bytes")
    for rel, want in expected.items():
        if want is not None and rel not in present:
            problems.append(f"{rel}: approved but missing")
    return problems


def _tier() -> str:
    from ossbot.ctl.hermes_render import tier_from_flag  # noqa: PLC0415

    return tier_from_flag()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ossbot.ctl.skills_root", description=__doc__.split("\n")[0])
    parser.add_argument("what", choices=("scan", "apply", "drift"))
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("skills_root must run as root", file=sys.stderr)
        return 2
    os.chdir("/")
    if args.what == "scan":
        doc = scan(_tier())
        print(json.dumps({"reviews": len(doc["reviews"]), "invalid": len(doc["invalid"])}))
        return 0
    if args.what == "apply":
        import pwd  # noqa: PLC0415

        done = apply_requests(gatekeeper_uid=pwd.getpwnam("gatekeeper").pw_uid)
        print(json.dumps([{k: v for k, v in d.items() if k != "files"} for d in done]))
        scan(_tier())
        return 0 if all(d.get("result") != "failed" for d in done) else 1
    problems = drift()
    print(json.dumps({"drift": problems}))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
