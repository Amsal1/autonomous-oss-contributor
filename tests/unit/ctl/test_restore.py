"""``ossctl restore`` (DESIGN §18.8, ADR-0016, PLAN item 43): a verified archive comes back with owners
and modes, the current state moves aside, the DB ends killed with dry-run ON and every open approval of
the three decision services invalidated; anything that does not match its manifest is refused."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import sqlite3
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ossbot.common.schemas import State
from ossbot.ctl import backup as bk
from ossbot.ctl import cli
from ossbot.ctl import restore as rs
from ossbot.gatekeeper.context import AppContext
from tests.conftest import insert_contribution, write_flag
from tests.unit.ctl.backup_helpers import (
    CID_ACTIVE,
    CID_DONE,
    IDENTITY,
    RECIPIENT,
    decrypt_tar,
    fake_age_bin,
    populate,
    reencrypt,
)
from tests.unit.gatekeeper.test_decision_fanout import _three_armed

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def layout(ctx: AppContext, tmp_path: Path) -> bk.Layout:
    insert_contribution(ctx, State.PLANNING, id=CID_ACTIVE)
    insert_contribution(ctx, State.SKIPPED, id=CID_DONE)
    return populate(ctx.paths, tmp_path)


def backup(layout: bk.Layout, tmp_path: Path) -> Path:
    return bk.create_archive(layout, bk.Options(), [RECIPIENT], tmp_path / "backups", keep=8, now=NOW,
                             age=fake_age_bin(tmp_path), staging_parent=tmp_path).archive  # fmt: skip


def restore(layout: bk.Layout, archive: Path, tmp_path: Path, identity: bytes = IDENTITY) -> tuple[dict, rs.DbReport]:  # type: ignore[type-arg]
    staging = tmp_path / "restore-staging"
    staging.mkdir(exist_ok=True)
    tar = staging / "archive.tar"
    rs.decrypt(archive, identity, tar, age=fake_age_bin(tmp_path))
    manifest = rs.unpack(tar, staging, layout)
    rs.install(manifest, staging, layout, rs.aside_dir(layout, NOW), attr=lambda flag, path: None)
    return manifest, rs.finalize_db(layout, "ossctl:restore", {"name": archive.name})


def q(path: Path, sql: str) -> list[tuple]:  # type: ignore[type-arg]
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_roundtrip_restores_the_state_killed_dry_run_on_and_approvals_invalidated(
    ctx: AppContext, layout: bk.Layout, tmp_path: Path
) -> None:
    ids = _three_armed(ctx)  # an armed PR approval, proposal approval and skill approval
    ctx.conn.execute("UPDATE control_state SET dry_run = 0")  # live mode at backup time
    archive = backup(layout, tmp_path)
    memory = layout.hermes_home / "memories" / "MEMORY.md"
    profile = layout.paths.config_dir / "profile.yaml"
    before = memory.read_text(), profile.read_text()
    memory.write_text("changed after the backup\n")  # later state, to be moved aside
    profile.unlink()
    (ctx.paths.resume_log).write_text('{"event": "operator_resume_offline"}\n')

    manifest, report = restore(layout, archive, tmp_path)

    assert (memory.read_text(), profile.read_text()) == before
    aside = rs.aside_dir(layout, NOW)
    assert (aside / str(memory).lstrip("/")).read_text() == "changed after the backup\n"  # moved, not deleted
    assert (aside / str(layout.paths.db_path).lstrip("/")).is_file()
    assert report.audit_ok and report.audit_rows > 0
    db_path = layout.paths.db_path
    [(state, dry_run, offset)] = q(db_path, "SELECT state, dry_run, resume_log_offset FROM control_state")
    assert (state, dry_run) == ("killed", 1)  # I7: a restored DB counts as a reset
    assert offset == ctx.paths.resume_log.stat().st_size  # old resume.log lines are not new offline resumes
    for table, approval_id in zip(("approvals", "proposal_approvals", "skill_approvals"), ids, strict=True):
        [(status, reason)] = q(db_path, f"SELECT status, invalidated_reason FROM {table} WHERE id = '{approval_id}'")
        assert (status, reason) == ("invalidated", "restored")
    assert set(report.invalidated) >= set(ids)
    [(data,)] = q(db_path, "SELECT data_json FROM audit_events WHERE event = 'restored'")
    assert json.loads(data)["dry_run"] is True
    # Hermes DBs and the snapshot pointer came back; modes as recorded
    assert q(layout.hermes_home / "ossbot-features" / "nutrition.db", "SELECT COUNT(*) FROM t") == [(3,)]
    assert (layout.paths.snapshot_root / "current").is_symlink()
    for path in (memory, profile):  # the repo's profile too: a 0600 file would lock the gatekeeper out
        entry = next(e for e in manifest["items"] if e["path"] == str(path))
        assert (path.stat().st_mode & 0o7777) == entry["mode"]


def _tampered(archive: Path, tmp_path: Path, change: object) -> Path:
    """Re-pack the decrypted archive after ``change(manifest, files)`` and encrypt it again."""
    with decrypt_tar(archive) as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}  # type: ignore[union-attr]
    manifest = json.loads(members.pop("MANIFEST.json"))
    change(manifest, members)  # type: ignore[operator]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as out:
        for name, data in [("MANIFEST.json", json.dumps(manifest).encode()), *members.items()]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    bad = tmp_path / "backups" / "ossbot-20261019-120001.tar.age"
    reencrypt(buf.getvalue(), bad)
    return bad


def _edit_file(manifest: dict, members: dict) -> None:  # type: ignore[type-arg]
    name = next(n for n in members if n.endswith("MEMORY.md"))
    members[name] = b"the owner likes coffee\n"


def _unknown_format(manifest: dict, members: dict) -> None:  # type: ignore[type-arg]
    manifest["format"] = 2


def _extra_member(manifest: dict, members: dict) -> None:  # type: ignore[type-arg]
    members["files/etc/cron.d/evil"] = b"* * * * * root sh"


def _outside_item(manifest: dict, members: dict) -> None:  # type: ignore[type-arg]
    entry = next(e for e in manifest["items"] if e["path"].endswith("MEMORY.md"))
    data = members.pop("files" + entry["path"])
    entry["path"] = entry["path"].replace("/memories/MEMORY.md", "/../../../etc/evil")
    members["files" + entry["path"]] = data


def _unknown_item(manifest: dict, members: dict) -> None:  # type: ignore[type-arg]
    manifest["items"][0]["item"] = "root_shell"


def _setuid(manifest: dict, members: dict) -> None:  # type: ignore[type-arg]
    manifest["items"][0]["mode"] = 0o4755


def _missing_member(manifest: dict, members: dict) -> None:  # type: ignore[type-arg]
    members.pop(next(n for n in members if n.endswith("MEMORY.md")))


@pytest.mark.parametrize("change", [_edit_file, _unknown_format, _extra_member, _outside_item, _unknown_item,
                                    _setuid, _missing_member], ids=lambda f: f.__name__.strip("_"))  # fmt: skip
def test_a_tampered_or_odd_archive_is_refused_before_anything_is_installed(
    layout: bk.Layout, tmp_path: Path, change: object
) -> None:
    archive = backup(layout, tmp_path)
    bad = _tampered(archive, tmp_path, change)
    memory = layout.hermes_home / "memories" / "MEMORY.md"
    memory.write_text("current\n")
    with pytest.raises(rs.RestoreError):
        restore(layout, bad, tmp_path)
    assert memory.read_text() == "current\n"  # nothing installed, nothing moved aside
    assert not rs.aside_dir(layout, NOW).exists()


def test_restore_never_writes_through_a_symlinked_hermes_home(layout: bk.Layout, tmp_path: Path) -> None:
    """F008-R1-1: the hermes uid swaps ``.hermes`` for a symlink to a root directory before the restore;
    nothing may be written into, or moved out of, the symlink's target."""
    archive = backup(layout, tmp_path)
    victim = tmp_path / "root-owned-dir"  # stands in for /etc
    victim.mkdir()
    (victim / "SOUL.md").write_text("root's file\n")
    layout.hermes_home.rename(tmp_path / "hermes-moved")
    os.symlink(victim, layout.hermes_home)
    with pytest.raises(rs.RestoreError, match=r"unsafe restore target: .*hermes.* (is a symlink|resolves to)"):
        restore(layout, archive, tmp_path)
    assert sorted(p.name for p in victim.iterdir()) == ["SOUL.md"]
    assert (victim / "SOUL.md").read_text() == "root's file\n"
    assert not rs.aside_dir(layout, NOW).exists()  # nothing moved aside


@pytest.mark.parametrize("case", ["other_owner", "missing_home", "symlinked_parent"])
def test_restore_refuses_unsafe_targets_before_anything_moves(layout: bk.Layout, tmp_path: Path, case: str) -> None:
    archive = backup(layout, tmp_path)
    if case == "other_owner":
        layout = dataclasses.replace(layout, hermes_user="root" if os.getuid() != 0 else "nobody")
        match = "is owned by uid"
    elif case == "missing_home":
        shutil.rmtree(layout.hermes_home)
        match = "provision Hermes first"
    else:  # the artifacts dir (another service uid's) swapped for a symlink: bundles:<cid> resolves elsewhere
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        layout.paths.artifacts_dir.rename(tmp_path / "artifacts-moved")
        os.symlink(elsewhere, layout.paths.artifacts_dir)
        match = "resolves to"
    with pytest.raises(rs.RestoreError, match=match):
        restore(layout, archive, tmp_path)
    assert not rs.aside_dir(layout, NOW).exists()
    assert (layout.paths.db_path).is_file()  # the current DB was not moved


@pytest.mark.parametrize("case", ["kill_failed", "stop_failed"])
def test_restore_fails_closed_when_the_kill_or_a_stop_failed(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """F008-R1-3: no kill flag, or a unit still running, means nothing is decrypted or moved; the
    gatekeeper and proxy are started again (killed mode when the flag exists)."""
    helper_calls: list[tuple[str, ...]] = []
    systemctl_calls: list[tuple[str, tuple[str, ...]]] = []

    def helper(name: str, *args: str, quiet: bool = False) -> int:
        helper_calls.append((name, *args))
        if case == "kill_failed":
            return 1  # ossbot-kill failed before it wrote the flag
        write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "restore", "actor": "ossctl-restore"})
        return 0

    def systemctl(action: str, units: tuple[str, ...]) -> list[str]:
        systemctl_calls.append((action, units))
        return ["ossbot-gatekeeper.service"] if action == "stop" and case == "stop_failed" else []

    def no_decrypt(*args: object, **kwargs: object) -> None:
        raise AssertionError("decrypt must not run after a failed kill or stop")

    monkeypatch.setattr(cli.Paths, "default", classmethod(lambda cls: ctx.paths))
    monkeypatch.setattr(cli, "_require_root", lambda what: None)
    monkeypatch.setattr(cli, "_root_helper", helper)
    monkeypatch.setattr(cli, "_systemctl", systemctl)
    monkeypatch.setattr(rs, "decrypt", no_decrypt)
    archive = tmp_path / "ossbot-20261019-120000.tar.age"
    archive.write_bytes(b"never decrypted here")

    result = CliRunner().invoke(cli.app, ["restore", str(archive), "--yes"], input=IDENTITY)

    assert result.exit_code == 1, result.output
    why = "no kill flag was written" if case == "kill_failed" else "did not stop: ossbot-gatekeeper.service"
    assert why in result.output
    assert helper_calls[0][:3] == ("ossbot-kill", "--soft", "--reason")
    assert systemctl_calls == [("stop", cli.RESTORE_STOP), ("start", cli.RESTORE_START)]
    assert not list(ctx.paths.srv.glob("restore-previous-*"))
    assert ctx.paths.db_path.is_file()


def test_a_wrong_or_missing_identity_is_refused(layout: bk.Layout, tmp_path: Path) -> None:
    archive = backup(layout, tmp_path)
    with pytest.raises(rs.RestoreError, match="no identity matched"):
        rs.decrypt(archive, b"FAKE-IDENTITY:age1someoneelse", tmp_path / "x.tar", age=fake_age_bin(tmp_path))
    with pytest.raises(rs.RestoreError, match="no age identity"):
        rs.decrypt(archive, b"  \n", tmp_path / "x.tar", age=fake_age_bin(tmp_path))
    assert not (tmp_path / "x.tar").exists()


def test_the_untouched_archive_passes_unpack(layout: bk.Layout, tmp_path: Path) -> None:
    archive = backup(layout, tmp_path)
    same = _tampered(archive, tmp_path, lambda manifest, members: None)
    staging = tmp_path / "s"
    staging.mkdir()
    rs.decrypt(same, IDENTITY, staging / "a.tar", age=fake_age_bin(tmp_path))
    assert rs.unpack(staging / "a.tar", staging, layout)["format"] == 1
