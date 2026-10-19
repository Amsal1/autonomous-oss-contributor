"""``ossctl restore <archive>``: put a ``ossctl backup`` archive back (DESIGN §18.8, ADR-0016, PLAN item 43).

Root only; ``ctl/cli.py`` writes ``killed`` (reason ``restore``) and stops the units first, then calls,
in order:

1. ``decrypt``: ``age -d -i -`` with the identity on stdin (it never touches the distro's disk) into a
   root-only temp dir on ext4.
2. ``unpack``: refuses an unknown ``format``, a member the manifest does not list (or a listed one that
   is missing), anything but regular files, a target outside the include list, and any file whose size
   or sha256 differs from the manifest.
3. ``check_targets`` (also the first step of ``install``): refuses, with nothing moved, an item root
   that is a symlink or has one above it and a Hermes home that is not a real directory of the hermes
   user (the hermes uid owns ``/home/hermes``; root must never write through a path it chose).
   ``install``: moves every current item aside to ``/srv/ossbot/restore-previous-<ts>/`` (never deletes;
   SQLite ``-wal``/``-shm``/``-journal`` siblings go with their database; the append-only flag of the
   anchors is lifted for the move), then writes the archive's files, directories and sibling symlinks
   with the owners and modes the manifest recorded.
4. ``finalize_db``: gatekeeper migrations, ``audit verify`` (restored chain vs restored anchors), every
   open approval of the three decision services invalidated (``restored``), ``dry_run = 1`` and state
   ``killed`` (I7: a restored DB counts as a reset), the resume.log offset moved to the current end (old
   lines are not new offline resumes), one ``restored`` audit event.

The caller re-applies ``chattr +a`` on the anchors and starts the gatekeeper in killed mode, so the
owner checks ``ossctl doctor`` and resumes on Telegram.
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import stat
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from ossbot.ctl.backup import AGE, FORMAT, LINK_TARGET_RE, SQLITE_SIBLINGS, Layout, hermes_kept, path_problem

MAX_IDENTITY = 64 * 1024
MAX_MANIFEST = 64 * 1024 * 1024
ENTRY_KEYS = {"path", "item", "mode", "owner", "group", "uid", "gid"}


class RestoreError(RuntimeError):
    """The archive is refused (nothing was installed when this is raised before ``install``)."""


def decrypt(archive: Path, identity: bytes, out_tar: Path, age: Path = AGE) -> None:
    if not identity.strip() or len(identity) > MAX_IDENTITY:
        raise RestoreError("no age identity on stdin (or larger than 64 KiB)")
    proc = subprocess.run([str(age), "-d", "-i", "-", "-o", str(out_tar), str(archive)], input=identity,
                          capture_output=True, check=False, timeout=3600)  # fmt: skip
    if proc.returncode != 0:
        out_tar.unlink(missing_ok=True)
        raise RestoreError(f"age -d failed (exit {proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[:300]}")


def item_root(layout: Layout, name: str) -> Path:
    """The path an archive item may write, by its manifest name (anything else is refused)."""
    p = layout.paths
    fixed = {
        "profile": p.config_dir / "profile.yaml",
        "telegram_config": p.config_dir / "telegram.yaml",
        "audit_anchors": p.audit_anchors_log,
        "gatekeeper_db": p.db_path,
        "config_snapshots": p.snapshot_root,
        "artifacts": p.artifacts_dir,
        "run_logs": layout.runs_dir,
        "skill_gate": layout.skills_root,
        "secrets": p.secrets_dir,
        "attachments": layout.attachments_dir,
    }
    if name in fixed:
        return fixed[name]
    kind, _, rest = name.partition(":")
    if kind in {"bundles", "imports"} and rest.startswith("c_") and rest[2:].isalnum() and len(rest) <= 42:
        return (p.artifacts_dir if kind == "bundles" else layout.imports_dir) / rest
    if kind == "hermes" and hermes_kept(rest):
        return layout.hermes_home / rest
    raise RestoreError(f"unknown archive item {name[:40]!r}")


def _check_entry(layout: Layout, entry: Any, *, file: bool) -> Path:
    if not isinstance(entry, dict) or not set(entry) >= ENTRY_KEYS:
        raise RestoreError("malformed manifest entry")
    raw = entry["path"]
    if not isinstance(raw, str) or not raw.startswith("/") or "\x00" in raw:
        raise RestoreError("manifest path is not absolute")
    pure = PurePosixPath(raw)
    if ".." in pure.parts or str(pure) != raw:
        raise RestoreError(f"manifest path not normalized: {raw[:80]!r}")
    root = item_root(layout, str(entry["item"]))
    path = Path(raw)
    if path != root and root not in path.parents:
        raise RestoreError(f"{raw[:80]!r} is outside its item {entry['item']!r}")
    mode = entry["mode"]
    # no setuid anywhere and no setgid on files; directories keep setgid (artifact dirs are 2750)
    if not isinstance(mode, int) or not 0 <= mode <= 0o7777 or mode & (0o6000 if file else 0o4000):
        raise RestoreError(f"bad mode for {raw[:80]!r}")
    if file and (not isinstance(entry.get("sha256"), str) or not isinstance(entry.get("size"), int)):
        raise RestoreError(f"no sha256/size for {raw[:80]!r}")
    return path


def unpack(tar_path: Path, staging: Path, layout: Layout) -> dict[str, Any]:
    """Validate the archive against its manifest and extract its files under ``staging/files``."""
    with tarfile.open(tar_path, "r:") as tar:
        members = {m.name: m for m in tar.getmembers()}
        head = members.get("MANIFEST.json")
        if head is None or not head.isfile() or head.size > MAX_MANIFEST:
            raise RestoreError("no MANIFEST.json")
        fh = tar.extractfile(head)
        if fh is None:
            raise RestoreError("no MANIFEST.json")
        try:
            manifest = json.loads(fh.read())
        except ValueError as exc:
            raise RestoreError("MANIFEST.json is not JSON") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            got = manifest.get("format") if isinstance(manifest, dict) else None
            raise RestoreError(f"unknown archive format {got!r} (this ossctl knows format {FORMAT})")
        if not all(isinstance(manifest.get(k), list) for k in ("items", "dirs", "links")):
            raise RestoreError("manifest without items/dirs/links lists")
        items: list[Any] = manifest["items"]
        dirs: list[Any] = manifest["dirs"]
        links: list[Any] = manifest["links"]
        for d in dirs:
            _check_entry(layout, d, file=False)
        for link in links:
            _check_entry(layout, link, file=False)
            if not isinstance(link.get("link"), str) or not LINK_TARGET_RE.fullmatch(link["link"]):
                raise RestoreError("manifest symlink target is not a sibling name")
        expected: dict[str, dict[str, Any]] = {}
        for entry in items:
            _check_entry(layout, entry, file=True)
            expected["files" + entry["path"]] = entry
        extra = sorted(set(members) - set(expected) - {"MANIFEST.json"})
        if extra:
            raise RestoreError(f"archive member not in the manifest: {extra[0][:80]!r}")
        missing = sorted(set(expected) - set(members))
        if missing:
            raise RestoreError(f"manifest file missing from the archive: {missing[0][:80]!r}")
        for name, entry in expected.items():
            member = members[name]
            if not member.isfile() or member.size != entry["size"]:
                raise RestoreError(f"{entry['path'][:80]!r}: not a regular file of the recorded size")
            src = tar.extractfile(member)
            if src is None:
                raise RestoreError(f"{entry['path'][:80]!r}: unreadable")
            dst = staging / "files" / entry["path"].lstrip("/")
            dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest = hashlib.sha256()
            with dst.open("wb") as out:
                for chunk in iter(lambda: src.read(1 << 20), b""):  # noqa: B023 - consumed in this iteration
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != entry["sha256"]:
                raise RestoreError(f"{entry['path'][:80]!r}: sha256 differs from the manifest (tampered or corrupt)")
    return manifest


def _uid(owner: str, uid: int) -> int:
    try:
        return pwd.getpwnam(owner).pw_uid
    except KeyError:
        return uid


def _gid(group: str, gid: int) -> int:
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError:
        return gid


def _own(path: Path, entry: dict[str, Any], *, link: bool = False, drvfs: bool = False) -> None:
    """Owner, group and mode from the manifest. The repo files (``config/profile.yaml``) sit on the drvfs
    mount: with its metadata option the recorded owner and mode apply there too (a root-created 0600 file
    would lock the gatekeeper out of its config, found live); without it chown fails and is ignored."""
    uid, gid = _uid(str(entry["owner"]), int(entry["uid"])), _gid(str(entry["group"]), int(entry["gid"]))
    try:
        os.chown(path, uid, gid, follow_symlinks=not link)
    except PermissionError:
        if os.geteuid() == 0 and not drvfs:
            raise
        # tests run unprivileged: owners other than the caller cannot be set there
    except OSError:
        if not drvfs:
            raise
    if not link:
        try:
            os.chmod(path, int(entry["mode"]))
        except OSError:
            if not drvfs:
                raise


Chattr = Callable[[str, Path], None]


def chattr(flag: str, path: Path) -> None:
    """``chattr +a|-a`` (root, ext4); a no-op where it is not supported (tests on tmpfs)."""
    if os.geteuid() != 0:
        return
    subprocess.run(["/usr/bin/chattr", flag, str(path)], check=False, capture_output=True)


def _move_aside(path: Path, aside: Path, moved: list[str]) -> None:
    for candidate in (path, *(path.with_name(path.name + s) for s in SQLITE_SIBLINGS)):
        if not os.path.lexists(candidate):
            continue
        dest = aside / str(candidate).lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.move(str(candidate), str(dest))
        moved.append(str(candidate))


def _roots(manifest: dict[str, Any], layout: Layout) -> list[Path]:
    roots: list[Path] = []
    for entry in [*manifest["items"], *manifest["dirs"], *manifest["links"]]:
        root = item_root(layout, str(entry["item"]))
        if root not in roots:
            roots.append(root)
    return roots


def check_targets(manifest: dict[str, Any], layout: Layout) -> None:
    """Refuse before anything moves when root would write through a symlink (F008-R1-1): no item root
    may be a symlink or have one above it, and the Hermes home must be a real directory of the hermes
    user. Called after the units are stopped (no hermes process can swap it any more) and again by
    ``install``."""
    for root in _roots(manifest, layout):
        problem = path_problem(root)
        if problem is not None:
            raise RestoreError(f"unsafe restore target: {problem}")
    entries = [*manifest["items"], *manifest["dirs"], *manifest["links"]]
    if any(str(e["item"]).startswith("hermes:") for e in entries):
        problem = layout.hermes_home_problem()
        if problem is not None:
            raise RestoreError(f"unsafe restore target: {problem}")
        if not os.path.lexists(layout.hermes_home):
            raise RestoreError(f"{layout.hermes_home} is missing: provision Hermes first (bootstrap), then restore")


def install(manifest: dict[str, Any], staging: Path, layout: Layout, aside: Path, *, attr: Chattr = chattr
            ) -> list[str]:  # fmt: skip
    """Move the current items aside, then write the archive's state. Returns the moved paths."""
    check_targets(manifest, layout)
    roots = _roots(manifest, layout)
    moved: list[str] = []
    for root in roots:
        if root == layout.paths.audit_anchors_log and root.exists():
            attr("-a", root)  # an append-only file cannot be renamed, even by root
        _move_aside(root, aside, moved)
    for d in sorted(manifest["dirs"], key=lambda e: len(PurePosixPath(e["path"]).parts)):
        path = Path(d["path"])
        path.mkdir(parents=True, exist_ok=True)
        _own(path, d)
    for entry in manifest["items"]:
        path = Path(entry["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        src = staging / "files" / entry["path"].lstrip("/")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "wb") as out, src.open("rb") as fin:
            shutil.copyfileobj(fin, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        _own(path, entry, drvfs=layout.paths.repo in path.parents)
    for link in manifest["links"]:
        path = Path(link["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(link["link"], path)
        _own(path, link, link=True)
    return moved


def _unused() -> NoReturn:
    raise RuntimeError("not needed to invalidate approvals")


def invalidate_open_decisions(conn: sqlite3.Connection, control_dir: Path, actor: str) -> list[str]:
    """Every pending, armed or confirmed approval of the three decision services (PR, proposal, skill)
    becomes ``invalidated`` with reason ``restored``, its tokens killed, each audited; in the caller's
    transaction."""
    from ossbot.common.clock import SystemClock  # noqa: PLC0415 - ossctl imports the gatekeeper lazily
    from ossbot.gatekeeper.approvals import ApprovalService  # noqa: PLC0415
    from ossbot.gatekeeper.audit import AuditLog  # noqa: PLC0415
    from ossbot.gatekeeper.decisions import DecisionServices  # noqa: PLC0415
    from ossbot.gatekeeper.proposal_approvals import ProposalApprovals  # noqa: PLC0415
    from ossbot.gatekeeper.skill_approvals import SkillApprovals  # noqa: PLC0415

    clock = SystemClock()
    audit = AuditLog(conn, clock)
    approvals = ApprovalService(conn, clock, audit, control_dir, _unused, _unused)
    services = DecisionServices(
        approvals=approvals,
        proposals=ProposalApprovals(conn, clock, audit, approvals, _unused, _unused, lambda: "restore"),
        skills=SkillApprovals(conn, clock, audit, approvals, _unused, _unused, lambda: "restore"),
    )
    return [i.approval_id for i in services.invalidate_all_open("restored", actor)]


def fix_db_owner(db_path: Path) -> None:
    """Files root created next to the gatekeeper DB (``-wal``, ``-shm``) get the DB file's owner."""
    st = db_path.stat()
    for sibling in (db_path.with_name(db_path.name + s) for s in SQLITE_SIBLINGS):
        if sibling.exists():
            try:
                os.chown(sibling, st.st_uid, st.st_gid)
            except PermissionError:
                if os.geteuid() == 0:
                    raise
            os.chmod(sibling, stat.S_IMODE(st.st_mode))


@dataclass
class DbReport:
    schema_version: int
    audit_ok: bool
    audit_rows: int
    audit_errors: list[str]
    invalidated: list[str]


def finalize_db(layout: Layout, actor: str, archive: dict[str, Any]) -> DbReport:
    """Migrations, audit verify, approvals invalidated, dry-run ON, killed, one ``restored`` event."""
    from ossbot.common.clock import SystemClock, to_iso  # noqa: PLC0415
    from ossbot.gatekeeper import db  # noqa: PLC0415
    from ossbot.gatekeeper.anchors import parse_anchors  # noqa: PLC0415
    from ossbot.gatekeeper.audit import AuditLog, verify  # noqa: PLC0415

    paths = layout.paths
    conn = db.connect(paths.db_path)
    try:
        version = db.migrate(conn)
        anchors, anchor_errors = parse_anchors(paths.audit_anchors_log)
        result = verify(conn, anchors)
        errors = [*anchor_errors, *result.errors]
        try:
            offset = paths.resume_log.stat().st_size
        except FileNotFoundError:
            offset = 0
        now = to_iso(SystemClock().now())
        with db.transaction(conn):
            invalidated = invalidate_open_decisions(conn, paths.control_dir, actor)
            conn.execute(
                "UPDATE control_state SET dry_run = 1, state = 'killed', reason = 'restore', changed_by = ?,"
                " changed_at = ?, resume_log_offset = ? WHERE id = 1",
                (actor, now, offset),
            )
            AuditLog(conn, SystemClock()).append(actor, "restored", {
                "archive": archive, "audit_ok": not errors, "invalidated": invalidated, "dry_run": True,
                "killed": True, "schema_version": version,
            })  # fmt: skip
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    fix_db_owner(paths.db_path)
    return DbReport(version, not errors, result.rows, errors[:20], invalidated)


def aside_dir(layout: Layout, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    return layout.paths.srv / f"restore-previous-{stamp}"
