"""``ossctl backup`` (DESIGN §18.8, ADR-0016, PLAN item 43): the include list, the manifest, always
encrypted, SQLite online copies, the audit check on the copy, retention."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ossbot.common.schemas import State
from ossbot.ctl import backup as bk
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_contribution
from tests.unit.ctl.backup_helpers import (
    CID_ACTIVE,
    CID_DONE,
    RECIPIENT,
    decrypt_tar,
    fake_age_bin,
    manifest_of,
    populate,
    sqlite_wal,
)

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def layout(ctx: AppContext, tmp_path: Path) -> bk.Layout:
    insert_contribution(ctx, State.PLANNING, id=CID_ACTIVE)
    insert_contribution(ctx, State.SKIPPED, id=CID_DONE)
    return populate(ctx.paths, tmp_path)


def make(layout: bk.Layout, tmp_path: Path, options: bk.Options | None = None) -> bk.Result:
    return bk.create_archive(layout, options or bk.Options(), [RECIPIENT], tmp_path / "backups", keep=8, now=NOW,
                             age=fake_age_bin(tmp_path), staging_parent=tmp_path)  # fmt: skip


def targets(manifest: dict) -> set[str]:  # type: ignore[type-arg]
    return {e["path"] for e in manifest["items"]}


def test_manifest_covers_every_included_item_and_nothing_else(layout: bk.Layout, tmp_path: Path) -> None:
    result = make(layout, tmp_path)
    assert result.archive.name == "ossbot-20261019-120000.tar.age"
    m = manifest_of(result.archive)
    assert m["format"] == 1 and m["includes_secrets"] is False and m["created_at"] == "2026-10-19T12:00:00Z"
    p, h = layout.paths, layout.hermes_home
    have = targets(m)
    for must in (p.config_dir / "profile.yaml", p.db_path, p.audit_anchors_log, h / "SOUL.md", h / "config.yaml",
                 h / "memories" / "MEMORY.md", h / "sessions" / "s1.json", h / "state.db",
                 h / "skills" / "git-helper" / "SKILL.md", h / "pending" / "skills" / "new" / "SKILL.md",
                 h / "ossbot-features" / "nutrition.db", layout.skills_root / "decisions.json",
                 p.artifacts_dir / CID_ACTIVE / "1" / "change.bundle",
                 layout.imports_dir / CID_ACTIVE / "change.bundle"):  # fmt: skip
        assert str(must) in have, must
    # Hermes code, tools, caches, logs, locks and the provisioned .env are left out
    for never in ("hermes-agent", "tools", "cache", "logs", ".env", "gateway.lock", "kanban.db.init.lock"):
        assert not any(t.startswith(str(h / never)) for t in have), never
    assert not any(CID_DONE in t for t in have)  # a terminal contribution's bundle is not kept
    assert not any(t.startswith(str(p.secrets_dir)) or t.startswith(str(layout.attachments_dir)) for t in have)
    assert not any(t.endswith(("-wal", "-shm", "-journal")) for t in have)
    assert any(link["path"] == str(p.snapshot_root / "current") for link in m["links"])  # the snapshot pointer
    assert m["audit"]["rows"] > 0 and m["audit"]["anchors_checked"] == 0
    # every file in the archive is listed with its sha256, and only those
    with decrypt_tar(result.archive) as tar:
        names = {n for n in tar.getnames() if n != "MANIFEST.json"}
        assert names == {"files" + t for t in have}
        for entry in m["items"]:
            fh = tar.extractfile("files" + entry["path"])
            assert fh is not None and hashlib.sha256(fh.read()).hexdigest() == entry["sha256"]
    owner = next(e for e in m["items"] if e["path"].endswith("MEMORY.md"))
    assert {"mode", "owner", "group", "uid", "gid", "size"} <= set(owner)


def test_the_archive_is_always_encrypted(layout: bk.Layout, tmp_path: Path) -> None:
    result = make(layout, tmp_path)
    raw = result.archive.read_bytes()
    assert b"the owner likes tea" not in raw and b"MANIFEST.json" not in raw
    assert not list(tmp_path.glob("ossbot-backup-*"))  # the plaintext staging dir is gone


@pytest.mark.parametrize("recipients", [[], ["age1replace-with-your-public-key"], ["ssh-ed25519 AAAA"],
                                        [RECIPIENT, "age1pq1" + "q" * 60]])  # fmt: skip
def test_recipients_are_required_and_well_formed(recipients: list[str]) -> None:
    with pytest.raises(bk.BackupError):
        bk.valid_recipients(recipients)


def test_opt_ins_add_secrets_artifacts_and_attachments(layout: bk.Layout, tmp_path: Path) -> None:
    m = manifest_of(make(layout, tmp_path, bk.Options(True, True, True)).archive)
    have = targets(m)
    assert str(layout.paths.secrets_dir / "gatekeeper.env") in have and m["includes_secrets"] is True
    assert str(layout.paths.artifacts_dir / CID_DONE / "1" / "change.bundle") in have  # every artifact
    assert any(d["path"] == str(layout.attachments_dir) for d in m["dirs"])


def test_sqlite_is_copied_online_with_the_rows_still_in_the_wal(layout: bk.Layout, tmp_path: Path) -> None:
    live = sqlite_wal(layout.hermes_home / "response_store.db", 7)  # open: the rows sit in the -wal file
    try:
        assert (layout.hermes_home / "response_store.db-wal").stat().st_size > 0
        result = make(layout, tmp_path)
    finally:
        live.close()
    with decrypt_tar(result.archive) as tar:
        fh = tar.extractfile("files" + str(layout.hermes_home / "response_store.db"))
        assert fh is not None
        copy = tmp_path / "copy.db"
        copy.write_bytes(fh.read())
    conn = sqlite3.connect(copy)
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 7
    conn.close()


def test_a_broken_audit_chain_aborts_and_writes_nothing(ctx: AppContext, layout: bk.Layout, tmp_path: Path) -> None:
    with db.transaction(ctx.conn):
        ctx.conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
        ctx.conn.execute("UPDATE audit_events SET actor = 'mallory' WHERE seq = 1")
    with pytest.raises(bk.BackupError, match="audit verify failed"):
        make(layout, tmp_path)
    assert not list((tmp_path / "backups").glob("*.tar.age"))
    assert not list(tmp_path.glob("ossbot-backup-*"))


def test_retention_keeps_the_newest_archives(tmp_path: Path) -> None:
    out = tmp_path / "b"
    out.mkdir()
    names = [f"ossbot-{(NOW + timedelta(days=i)):%Y%m%d-%H%M%S}.tar.age" for i in range(10)]
    for n in names:
        (out / n).write_bytes(b"x")
    (out / "notes.txt").write_text("keep me")
    deleted = bk.retention(out, 8)
    assert sorted(deleted) == names[:2]
    assert sorted(p.name for p in out.iterdir()) == sorted([*names[2:], "notes.txt"])


def test_only_sibling_symlinks_are_kept(layout: bk.Layout, tmp_path: Path) -> None:
    os.symlink("/etc/passwd", layout.hermes_home / "memories" / "evil")
    m = manifest_of(make(layout, tmp_path).archive)
    assert not any(link["path"].endswith("evil") for link in m["links"])
    assert any("evil" in s for s in m["skipped"])


def _swap_for_symlink(path: Path, target: Path) -> None:
    """What the uid owning ``path`` can do at any time: move it away and put a symlink in its place."""
    path.rename(path.with_name(path.name + ".real"))
    os.symlink(target, path)


def test_a_symlinked_hermes_home_is_refused_not_followed(layout: bk.Layout, tmp_path: Path) -> None:
    """F008-R1-1: ``.hermes`` -> the secrets dir must not put secrets into an includes_secrets=false archive."""
    _swap_for_symlink(layout.hermes_home, layout.paths.secrets_dir)
    with pytest.raises(bk.BackupError, match=r"Hermes home refused: .* is a symlink"):
        make(layout, tmp_path)
    assert not list((tmp_path / "backups").glob("*.tar.age"))
    assert not list(tmp_path.glob("ossbot-backup-*"))  # the staging dir is gone too


def test_a_hermes_home_of_another_owner_is_refused(layout: bk.Layout, tmp_path: Path) -> None:
    other = "root" if os.getuid() != 0 else "nobody"
    with pytest.raises(bk.BackupError, match=f"not {other}"):
        make(dataclasses.replace(layout, hermes_user=other), tmp_path)


def test_an_item_root_below_a_symlinked_directory_is_refused(layout: bk.Layout, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _swap_for_symlink(layout.imports_dir, elsewhere)  # imports/<cid> now resolves outside the gatekeeper dir
    with pytest.raises(bk.BackupError, match=rf"imports:{CID_ACTIVE} refused: .* resolves to"):
        make(layout, tmp_path)


def test_a_file_opened_through_a_swapped_directory_is_refused(tmp_path: Path) -> None:
    """The walk lists a directory, then the owner swaps it for a symlink before the file is opened."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "f").write_text("x")
    os.symlink(real, tmp_path / "swapped")
    with pytest.raises(bk.BackupError, match="a symlink above it"):
        bk._copy_file(tmp_path / "swapped" / "f", tmp_path / "out" / "f")
    sqlite_wal(real / "s.db", 1).close()
    with pytest.raises(bk.BackupError, match="a symlink above it"):
        bk._copy_file(tmp_path / "swapped" / "s.db", tmp_path / "out" / "s.db")


def test_a_directory_swapped_and_back_during_the_sqlite_copy_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F008-R2-1: sqlite reopens by path, so the owner swaps a directory above the DB for a symlink to
    another DB just for sqlite's open and back before the post-copy check; the copy is still refused."""
    home, other = tmp_path / "home", tmp_path / "other"
    home.mkdir()
    other.mkdir()
    sqlite_wal(home / "s.db", 1).close()
    sqlite_wal(other / "s.db", 2).close()  # "another root-readable database"
    real_connect = sqlite3.connect

    def swapping_connect(database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        if not str(database).startswith(f"file:{home}/"):
            return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]
        home.rename(tmp_path / "home.real")
        os.symlink(other, home)
        try:
            conn = real_connect(database, *args, **kwargs)  # type: ignore[arg-type]
            conn.execute("SELECT 1 FROM sqlite_master").fetchall()  # the open happens inside the window
        finally:
            home.unlink()
            (tmp_path / "home.real").rename(home)
        return conn

    monkeypatch.setattr(sqlite3, "connect", swapping_connect)
    with pytest.raises(bk.BackupError, match="refused"):
        bk._copy_file(home / "s.db", tmp_path / "out" / "s.db")
    assert not (tmp_path / "out" / "s.db").exists()


def test_the_manifest_mode_comes_from_the_copied_file(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    (tmp_path / "f").chmod(0o640)
    st = bk._copy_file(tmp_path / "f", tmp_path / "out" / "f")
    assert stat.S_IMODE(st.st_mode) == 0o640 and (tmp_path / "out" / "f").read_text() == "x"


def test_a_missing_gatekeeper_db_or_anchors_is_an_error(layout: bk.Layout, tmp_path: Path) -> None:
    layout.paths.audit_anchors_log.unlink()
    with pytest.raises(bk.BackupError, match="audit_anchors"):
        make(layout, tmp_path)
