"""``ossctl backup``: one age-encrypted archive of the state outside git (DESIGN §18.8, ADR-0016,
PLAN item 43). Root only; ``ctl/cli.py`` adds the pause/resume around it and records the audit event.

What goes in (``include_list``): the §18.8 table plus the Hermes state layout from FEAT-007:

* always: ``config/profile.yaml``, ``config/telegram.yaml`` (when present), the audit anchors (copied
  before the DB, so every anchor names a row the DB copy has), the gatekeeper DB, the config snapshots,
  the bundle artifacts and operator imports of non-terminal contributions, the Hermes state: every
  top-level entry of ``/home/hermes/.hermes`` except code and caches (``HERMES_EXCLUDE``; so memories,
  sessions, ``SOUL.md``, ``config.yaml``, the state and response DBs, ``skills/`` = the active skills,
  ``pending/``, ``ossbot-features/`` = the feature stores, cron and kanban state), and the root skill
  gate state ``/var/lib/ossbot/skills``;
* opt-in: ``/etc/ossbot/secrets`` (``--include-secrets`` or ``policy.backup.include_secrets``), every
  artifact and run log (``--include-artifacts``), the transient chat attachments
  (``--include-attachments``; excluded by default, they expire after 24 h);
* never: Hermes code, installs, tools, plugins, hooks, caches, logs, locks, runtime files and ``.env``
  (re-provisioned), the sandboxd DB, the probe results, Docker data, the VHDs.

Every SQLite file is copied with the online backup API (``sqlite3.Connection.backup``); ``-wal``,
``-shm`` and ``-journal`` siblings are never copied. Each file is copied into a root-only staging dir
first and hashed there, so the manifest describes exactly the bytes in the archive. Nothing is read
through a symlink: an item root with a symlink in its path, a Hermes home that is not a real directory
of the hermes user (or is replaced during the copy) and a file whose opened path differs abort the
backup (``path_problem``, ``Layout.hermes_home_problem``, ``_open_exact``). The archive is a
tar of ``MANIFEST.json`` + ``files/<absolute path>``, always encrypted with ``age`` to the X25519 (or
post-quantum hybrid) recipients in ``profile.backup.age_recipients``; there is no unencrypted mode.
"""

from __future__ import annotations

import grp
import hashlib
import io
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from ossbot.common.paths import Paths

FORMAT = 1
AGE = Path("/usr/local/bin/age")
STAGING_PARENT = Path("/var/tmp")  # noqa: S108 - mkdtemp below: a fresh 0700 dir of root's, on ext4
ARCHIVE_RE = re.compile(r"^ossbot-\d{8}-\d{6}\.tar\.age$")
SQLITE_MAGIC = b"SQLite format 3\x00"
SQLITE_SIBLINGS = ("-wal", "-shm", "-journal")
# age 1.3: classic X25519 recipients (age1 + 58 bech32 chars) and post-quantum hybrid ones (age1pq1...).
RECIPIENT_RE = re.compile(r"^age1(?:pq1)?[02-9ac-hj-np-z]{58,}$")
LINK_TARGET_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")  # a sibling name only (config-snapshot/current)
HERMES_HOME = Path("/home/hermes/.hermes")
HERMES_USER = "hermes"
# DESIGN §18.8: "/home/hermes/.hermes/ minus hermes-agent/ code and caches". Kept: every top-level entry
# but these (code, installs, tools and plugins are re-provisioned; .env is the root-owned provisioned
# key file; caches, logs, locks and the gateway's runtime files are rebuilt). The FEAT-007 state list
# (config.yaml, SOUL.md, memories/, sessions/, state.db, response_store.db, skills/ = the active skills,
# pending/, ossbot-features/ = the feature stores) is a subset of what this keeps.
HERMES_EXCLUDE = frozenset({"hermes-agent", "installs", "tools", "plugins", "hooks", "cache", "audio_cache",
                            "image_cache", "models_dev_cache.json", "models_dev_cache.etag", "logs", "kiro-cwd",
                            "gateway.pid", "gateway.lock", "gateway.sock", "gateway_state.json",
                            "gateway-starts.log"})  # fmt: skip
HERMES_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def hermes_kept(name: str) -> bool:
    """A top-level name under ~hermes/.hermes that the backup keeps (and a restore may write)."""
    return (bool(HERMES_NAME_RE.fullmatch(name)) and name not in HERMES_EXCLUDE and not name.endswith(".lock")
            and not name.endswith(SQLITE_SIBLINGS))  # fmt: skip


Flag = Literal["", "secrets", "artifacts", "attachments"]


class BackupError(RuntimeError):
    """The backup cannot be made (no recipients, audit chain broken, age failed...)."""


def path_problem(path: Path) -> str | None:
    """Why root must not read or write through ``path`` (None: it may). Backup and restore run as root on
    paths whose directories other uids own (``~hermes/.hermes`` above all, writable by the hermes uid):
    the path itself must not be a symlink and no existing component above it may be one, i.e.
    ``realpath`` gives back the same path. A missing path is fine (nothing to copy, or created fresh)."""
    if os.path.islink(path):
        return f"{path} is a symlink"
    real = os.path.realpath(path)
    if real != str(path):
        return f"{path} resolves to {real[:160]} (a symlink above it)"
    return None


@dataclass(frozen=True)
class Layout:
    """Where the state lives (``Layout.default()``); tests relocate it under a tmp dir."""

    paths: Paths
    hermes_home: Path = HERMES_HOME
    hermes_user: str = HERMES_USER  # the owner the Hermes home must have

    @classmethod
    def default(cls) -> Layout:
        return cls(Paths.default())

    @property
    def skills_root(self) -> Path:
        return self.paths.var_lib / "skills"

    @property
    def imports_dir(self) -> Path:
        return self.paths.gatekeeper_dir / "imports"

    @property
    def attachments_dir(self) -> Path:
        return self.paths.gatekeeper_dir / "chat"

    @property
    def runs_dir(self) -> Path:
        return self.paths.logs_dir / "runs"

    def hermes_home_problem(self) -> str | None:
        """Why the Hermes home must not be used (None: it is a real directory of ``hermes_user``, or
        absent). The hermes uid may replace ``.hermes`` with a symlink at any time (ReadWritePaths
        /home/hermes): followed by root, a backup would copy files from elsewhere and a restore would
        write Hermes-chosen files there (F008-R1-1)."""
        home = self.hermes_home
        problem = path_problem(home)
        if problem is not None:
            return problem
        try:
            st = os.lstat(home)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            return f"{home} is not a directory"
        try:
            uid = pwd.getpwnam(self.hermes_user).pw_uid
        except KeyError:
            return f"{home}: no user {self.hermes_user!r} to own it"
        if st.st_uid != uid:
            return f"{home} is owned by uid {st.st_uid}, not {self.hermes_user}"
        return None

    def hermes_identity(self) -> tuple[int, int] | None:
        """(st_dev, st_ino) of the Hermes home, None when absent; compared before and after a copy."""
        try:
            st = os.lstat(self.hermes_home)
        except FileNotFoundError:
            return None
        return st.st_dev, st.st_ino

    def hermes_names(self) -> list[str]:
        """The kept top-level entries that exist now (sorted); refuses an unsafe Hermes home."""
        problem = self.hermes_home_problem()
        if problem is not None:
            raise BackupError(f"Hermes home refused: {problem}")
        try:
            return sorted(n for n in os.listdir(self.hermes_home) if hermes_kept(n))
        except FileNotFoundError:
            return []


@dataclass(frozen=True)
class Item:
    name: str
    path: Path
    required: bool = False
    flag: Flag = ""


@dataclass(frozen=True)
class Options:
    include_secrets: bool = False
    include_artifacts: bool = False
    include_attachments: bool = False

    def wants(self, flag: Flag) -> bool:
        return flag == "" or getattr(self, f"include_{flag}")


@dataclass
class Result:
    archive: Path
    sha256: str
    size: int
    files: int
    manifest: dict[str, Any]
    deleted: list[str] = field(default_factory=list)


def include_list(layout: Layout, active_cids: Iterable[str], options: Options) -> list[Item]:
    """The §18.8 include list in copy order (anchors before the DB, see the module docstring)."""
    p = layout.paths
    items = [
        Item("profile", p.config_dir / "profile.yaml"),
        Item("telegram_config", p.config_dir / "telegram.yaml"),
        Item("audit_anchors", p.audit_anchors_log, required=True),
        Item("gatekeeper_db", p.db_path, required=True),
        Item("config_snapshots", p.snapshot_root),
    ]
    if options.include_artifacts:
        items += [Item("artifacts", p.artifacts_dir, flag="artifacts"),
                  Item("run_logs", layout.runs_dir, flag="artifacts")]  # fmt: skip
    cids = sorted(set(active_cids))
    if not options.include_artifacts:
        items += [Item(f"bundles:{cid}", p.artifacts_dir / cid) for cid in cids]
    items += [Item(f"imports:{cid}", layout.imports_dir / cid) for cid in cids]
    items += [Item(f"hermes:{name}", layout.hermes_home / name) for name in layout.hermes_names()]
    items.append(Item("skill_gate", layout.skills_root))
    items.append(Item("secrets", p.secrets_dir, flag="secrets"))
    items.append(Item("attachments", layout.attachments_dir, flag="attachments"))
    return [i for i in items if options.wants(i.flag)]


def valid_recipients(recipients: Sequence[str]) -> list[str]:
    """The profile's age recipients, all of them well-formed; refuses an empty list or a placeholder."""
    out = [r.strip() for r in recipients]
    if not out:
        raise BackupError("no backup recipients: set profile.backup.age_recipients to your age public key(s) "
                          "(age-keygen, SETUP.md); archives are always encrypted")  # fmt: skip
    bad = [r[:16] for r in out if not RECIPIENT_RE.fullmatch(r)]
    if bad:
        raise BackupError(f"not an age recipient: {', '.join(bad)}… (expected age1… public keys)")
    if len({r.startswith("age1pq1") for r in out}) > 1:
        raise BackupError("age cannot encrypt to classic and post-quantum recipients at once")
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _names(st: os.stat_result) -> tuple[str, str]:
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return owner, group


def _meta(path: Path, st: os.stat_result, item: str) -> dict[str, Any]:
    owner, group = _names(st)
    return {"path": str(path), "item": item, "mode": stat.S_IMODE(st.st_mode), "owner": owner, "group": group,
            "uid": st.st_uid, "gid": st.st_gid}  # fmt: skip


def _refuse_moved(path: Path) -> None:
    problem = path_problem(path)
    if problem is not None:
        raise BackupError(f"refused: {problem} (changed during the backup?)")


def _open_exact(path: Path) -> int:
    """Open ``path`` read-only, refusing it unless the opened file is that very path: O_NOFOLLOW guards
    the last component, the fd's /proc link every directory above it (a directory swapped for a
    symlink between the walk and the open)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        real = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        real = os.path.realpath(path)
    if real != str(path):
        os.close(fd)
        raise BackupError(f"refused: {path} opened {real[:160]} (a symlink above it)")
    return fd


def _open_fds() -> set[int]:
    """The descriptors open now; the listing's own (closed again when listdir returns) is left out, so
    sqlite reusing its number does not hide the file it opened."""
    out: set[int] = set()
    for name in os.listdir("/proc/self/fd"):
        try:
            os.fstat(int(name))
        except OSError:
            continue
        out.add(int(name))
    return out


def _verify_sqlite_fds(src: Path, exact: os.stat_result, before: set[int]) -> None:
    """sqlite opens ``src`` (and its ``-wal``/``-shm``) by path: every file it opened must be that path,
    and its main database must be the very inode ``_open_exact`` checked (F008-R2-1: a directory above
    swapped for a symlink and back during the copy must not make root copy another database)."""
    main = False
    after = _open_fds()
    for fd in after - before:
        try:
            real = os.readlink(f"/proc/self/fd/{fd}")
            st = os.fstat(fd)
        except OSError:
            continue  # the listing's own fd, already closed
        if not stat.S_ISREG(st.st_mode):
            continue
        if real not in {str(src), *(f"{src}{s}" for s in SQLITE_SIBLINGS)}:
            raise BackupError(f"refused: copying {src} opened {real[:160]} (a symlink above it?)")
        if real == str(src):
            if (st.st_dev, st.st_ino) != (exact.st_dev, exact.st_ino):
                raise BackupError(f"refused: {src} was replaced during the backup")
            main = True
    if not main:
        raise BackupError(f"refused: cannot verify the file sqlite opened for {src}")


def _copy_file(src: Path, dst: Path) -> os.stat_result:
    """Copy ``src`` to ``dst``; returns the ``fstat`` of the verified source file (the manifest's owner
    and mode come from it, never from a second lookup by path)."""
    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(_open_exact(src), "rb") as fin:
        exact = os.fstat(fin.fileno())
        if fin.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC:
            before = _open_fds()
            source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
            target = None
            try:
                source.execute("SELECT count(*) FROM sqlite_master").fetchone()  # the files are open now
                _verify_sqlite_fds(src, exact, before)
                target = sqlite3.connect(dst)
                source.backup(target)  # consistent copy of a live WAL database
            finally:
                if target is not None:
                    target.close()
                source.close()
            _refuse_moved(src)  # sqlite opens by path: the path must still be real after the copy
            return exact
        fin.seek(0)
        with dst.open("wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        return exact


class Collector:
    """Copies items into ``staging/files/<absolute path>`` and records their manifest entries."""

    def __init__(self, staging: Path) -> None:
        self.root = staging / "files"
        self.files: list[dict[str, Any]] = []
        self.dirs: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self.absent: list[str] = []
        self.skipped: list[str] = []

    def staged(self, target: Path) -> Path:
        return self.root / str(target).lstrip("/")

    def add(self, item: Item) -> None:
        problem = path_problem(item.path)
        if problem is not None:  # an item root (or a directory above it) swapped for a symlink
            raise BackupError(f"{item.name} refused: {problem}")
        try:
            st = os.lstat(item.path)
        except FileNotFoundError:
            if item.required:
                raise BackupError(f"{item.name}: {item.path} is missing") from None
            self.absent.append(item.name)
            return
        if stat.S_ISDIR(st.st_mode):
            self._dir(item.path, item.name)
        elif stat.S_ISREG(st.st_mode):
            self._file(item.path, item.name)
        else:
            self.skipped.append(f"{item.path} (not a regular file or directory)")

    def _file(self, path: Path, item: str) -> None:
        if path.name.endswith(SQLITE_SIBLINGS):
            return  # captured by the online backup of the database itself
        dst = self.staged(path)
        entry = _meta(path, _copy_file(path, dst), item)
        entry.update(sha256=sha256_file(dst), size=dst.stat().st_size)
        self.files.append(entry)

    def _dir(self, root: Path, item: str) -> None:
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            here = Path(current)
            _refuse_moved(here)  # os.walk descends by path: a subdirectory swapped for a symlink meanwhile
            self.dirs.append(_meta(here, os.lstat(here), item))
            for name in sorted(dirnames):
                sub = here / name
                if os.path.islink(sub):  # os.walk lists symlinked dirs among dirnames; never followed
                    self._link(sub, item)
            dirnames[:] = sorted(d for d in dirnames if not os.path.islink(here / d))
            for name in sorted(filenames):
                path = here / name
                st = os.lstat(path)
                if stat.S_ISLNK(st.st_mode):
                    self._link(path, item)
                elif stat.S_ISREG(st.st_mode):
                    self._file(path, item)
                else:
                    self.skipped.append(f"{path} (special file)")

    def _link(self, path: Path, item: str) -> None:
        target = os.readlink(path)
        if not LINK_TARGET_RE.fullmatch(target) or target in {".", ".."}:
            self.skipped.append(f"{path} (symlink to {target[:60]!r}: only sibling names are kept)")
            return
        entry = _meta(path, os.lstat(path), item)
        entry["link"] = target
        self.links.append(entry)


def git_head(repo: Path) -> str:
    """The repo HEAD commit without running git (root on a drvfs repo owned by ossadmin)."""
    try:
        head = (repo / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:40]
        ref = head[5:]
        loose = repo / ".git" / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()[:40]
        for line in (repo / ".git" / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0][:40]
    except OSError:
        pass
    return "unknown"


def ossbot_version() -> str:
    try:
        return metadata.version("ossbot")
    except metadata.PackageNotFoundError:
        return "unknown"


def verify_copy(collector: Collector, layout: Layout) -> dict[str, Any]:
    """``audit verify`` on the DB copy against the copied anchors; a broken chain aborts the backup."""
    from ossbot.gatekeeper.anchors import parse_anchors  # noqa: PLC0415 - ossctl imports the gatekeeper lazily
    from ossbot.gatekeeper.audit import verify  # noqa: PLC0415

    anchors, anchor_errors = parse_anchors(collector.staged(layout.paths.audit_anchors_log))
    conn = sqlite3.connect(f"file:{collector.staged(layout.paths.db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = verify(conn, anchors)
    finally:
        conn.close()
    errors = [*anchor_errors, *result.errors]
    if errors or not result.ok:
        raise BackupError(f"audit verify failed on the DB copy: {'; '.join(errors[:5])}")
    return {"rows": result.rows, "head_seq": result.head.seq, "anchors_checked": result.anchors_checked}


def active_cids(db_copy: Path) -> list[str]:
    """Non-terminal contributions in the DB copy (their bundles and imports are kept)."""
    from ossbot.common.schemas import TERMINAL_STATES  # noqa: PLC0415

    terminal = sorted(s.value for s in TERMINAL_STATES)
    conn = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(terminal))
        rows = conn.execute(f"SELECT id FROM contributions WHERE state NOT IN ({marks})", terminal).fetchall()  # noqa: S608 - placeholders only
    finally:
        conn.close()
    return [str(r[0]) for r in rows if re.fullmatch(r"c_[A-Za-z0-9]{1,40}", str(r[0]))]


def collect(layout: Layout, options: Options, staging: Path) -> Collector:
    """Copy everything into ``staging``: the always-present core first, then the per-contribution items
    the DB copy names."""
    collector = Collector(staging)
    core = [i for i in include_list(layout, (), options) if i.name in {"audit_anchors", "gatekeeper_db"}]
    for item in core:
        collector.add(item)
    cids = active_cids(collector.staged(layout.paths.db_path))
    hermes_before = layout.hermes_identity()
    for item in include_list(layout, cids, options):  # include_list refuses an unsafe Hermes home
        if item.name not in {"audit_anchors", "gatekeeper_db"}:
            collector.add(item)
    # Hermes keeps running during a backup: its home must still be the directory that was checked
    problem = layout.hermes_home_problem()
    if problem is None and layout.hermes_identity() != hermes_before:
        problem = f"{layout.hermes_home} was replaced"
    if problem is not None:
        raise BackupError(f"Hermes home changed during the backup: {problem}")
    return collector


def manifest_doc(collector: Collector, layout: Layout, options: Options, audit: dict[str, Any], now: datetime
                 ) -> dict[str, Any]:  # fmt: skip
    return {
        "format": FORMAT,
        "created_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ossbot_version": ossbot_version(),
        "git_head": git_head(layout.paths.repo),
        "includes_secrets": options.include_secrets,
        "includes_artifacts": options.include_artifacts,
        "includes_attachments": options.include_attachments,
        "audit": audit,
        "absent": collector.absent,
        "skipped": collector.skipped,
        "items": collector.files,
        "dirs": collector.dirs,
        "links": collector.links,
    }


def write_tar(collector: Collector, manifest: dict[str, Any], tar_path: Path) -> None:
    """Plain tar (not compressed: SQLite and bundles barely compress, age adds integrity). Members are
    root-owned 0600 containers; owners and modes live in the manifest."""
    data = json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8")
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("MANIFEST.json")
        info.size, info.mode, info.mtime = len(data), 0o600, int(datetime.now(UTC).timestamp())
        tar.addfile(info, io.BytesIO(data))
        for entry in manifest["items"]:
            src = collector.staged(Path(entry["path"]))
            info = tarfile.TarInfo("files" + entry["path"])
            info.size, info.mode, info.mtime = src.stat().st_size, 0o600, int(src.stat().st_mtime)
            with src.open("rb") as fh:
                tar.addfile(info, fh)


def encrypt(tar_path: Path, out: Path, recipients: Sequence[str], age: Path = AGE) -> None:
    argv = [str(age), "-e"]
    for r in recipients:
        argv += ["-r", r]
    argv += ["-o", str(out), str(tar_path)]
    proc = subprocess.run(argv, capture_output=True, check=False, timeout=3600)
    if proc.returncode != 0:
        out.unlink(missing_ok=True)
        raise BackupError(f"age -e failed (exit {proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[:300]}")


def retention(out_dir: Path, keep: int) -> list[str]:
    """Keep the newest ``keep`` archives (by their timestamped name); delete only ossbot-*.tar.age."""
    archives = sorted((p.name for p in out_dir.iterdir() if p.is_file() and ARCHIVE_RE.fullmatch(p.name)), reverse=True)
    deleted = archives[max(keep, 1) :]
    for name in deleted:
        (out_dir / name).unlink(missing_ok=True)
    return deleted


def create_archive(
    layout: Layout, options: Options, recipients: Sequence[str], out_dir: Path, *, keep: int,
    now: datetime | None = None, age: Path = AGE, staging_parent: Path | None = None,
) -> Result:  # fmt: skip
    """Collect, verify, tar, encrypt, keep the newest ``keep``. The plaintext stays in a root-only temp
    dir on ext4 and is removed before returning, whatever happens."""
    recipients = valid_recipients(recipients)
    now = now or datetime.now(UTC)
    out_dir.mkdir(parents=True, exist_ok=True)
    # mode 0700, on ext4 (/var/tmp), never under the drvfs repo
    staging = Path(tempfile.mkdtemp(prefix="ossbot-backup-", dir=staging_parent or STAGING_PARENT))
    try:
        collector = collect(layout, options, staging)
        audit = verify_copy(collector, layout)
        manifest = manifest_doc(collector, layout, options, audit, now)
        tar_path = staging / "archive.tar"
        write_tar(collector, manifest, tar_path)
        name = f"ossbot-{now.astimezone(UTC):%Y%m%d-%H%M%S}.tar.age"
        part = out_dir / f".{name}.part"
        encrypt(tar_path, part, recipients, age)
        final = out_dir / name
        os.replace(part, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    deleted = retention(out_dir, keep)
    return Result(final, sha256_file(final), final.stat().st_size, len(manifest["items"]), manifest, deleted)
