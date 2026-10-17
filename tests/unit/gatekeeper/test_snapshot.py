"""Config snapshot: loaded only at start or on a confirmed reload (DESIGN §7.9)."""

from __future__ import annotations

import json
import os

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.config import ConfigError
from ossbot.common.paths import Paths
from ossbot.common.snapshot import read_current, snapshot_hash
from ossbot.gatekeeper import db
from ossbot.gatekeeper.config_snapshot import ReloadConflict, read_config_files
from ossbot.gatekeeper.context import AppContext, build_context
from tests.conftest import TEST_ROLES, packaged_contribution


def edit_policy(ctx: AppContext) -> None:
    path = ctx.paths.config_dir / "policy.yaml"
    path.write_text(path.read_text().replace("comments_per_24h: 10", "comments_per_24h: 9"))


def test_start_writes_snapshot_and_records_it(ctx: AppContext) -> None:
    digest, files = read_current(ctx.paths.snapshot_root)
    assert digest == ctx.snapshots.current.hash == snapshot_hash(read_config_files(ctx.paths))
    assert set(files) == {"policy.yaml", "targets.yaml", "egress.yaml", "profile.yaml"}
    row = db.one(ctx.conn, "SELECT hash FROM config_snapshots WHERE hash = ?", (digest,))
    assert row is not None
    loaded = ctx.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event = 'config_loaded'").fetchone()[0]
    assert loaded == 1


def test_editing_drvfs_without_reload_changes_nothing(ctx: AppContext) -> None:
    before = ctx.snapshots.current.hash
    edit_policy(ctx)
    assert ctx.snapshots.current.hash == before
    assert ctx.config.policy.limits.comments_per_24h == 10
    assert os.readlink(ctx.paths.snapshot_root / "current") == before


def test_reload_is_two_step_with_confirm_hash(ctx: AppContext) -> None:
    edit_policy(ctx)
    preview = ctx.snapshots.prepare_reload()
    assert preview.changed_files == ("policy.yaml",)
    assert "-  comments_per_24h: 10" in preview.diff and "+  comments_per_24h: 9" in preview.diff
    assert ctx.config.policy.limits.comments_per_24h == 10  # preview applies nothing
    snap, _ = ctx.snapshots.apply_reload(preview.hash, "uid:1000/operator")
    assert ctx.config.policy.limits.comments_per_24h == 9
    assert db.current_policy_hash(ctx.conn) == snap.hash
    assert os.readlink(ctx.paths.snapshot_root / "current") == snap.hash


def test_reload_rejects_changed_files_after_diff(ctx: AppContext) -> None:
    edit_policy(ctx)
    preview = ctx.snapshots.prepare_reload()
    path = ctx.paths.config_dir / "policy.yaml"
    path.write_text(path.read_text().replace("issue_claims_per_24h: 2", "issue_claims_per_24h: 5"))
    with pytest.raises(ReloadConflict):
        ctx.snapshots.apply_reload(preview.hash, "uid:1000/operator")
    assert ctx.config.policy.limits.issue_claims_per_24h == 2


def test_reload_unchanged_is_conflict(ctx: AppContext) -> None:
    preview = ctx.snapshots.prepare_reload()
    with pytest.raises(ReloadConflict):
        ctx.snapshots.apply_reload(preview.hash, "uid:1000/operator")


def test_invalid_reload_keeps_previous_snapshot(ctx: AppContext) -> None:
    before = ctx.snapshots.current.hash
    (ctx.paths.config_dir / "targets.yaml").write_text("version: 1\nbogus: true\n")
    with pytest.raises(ConfigError):
        ctx.snapshots.prepare_reload()
    assert ctx.snapshots.current.hash == before


def test_snapshot_hash_change_invalidates_approvals(ctx: AppContext) -> None:
    _, action = packaged_contribution(ctx)
    approval = ctx.approvals.create(action, tg_chat_id=4242, body_parts_total=1)
    edit_policy(ctx)
    ctx.snapshots.apply_reload(ctx.snapshots.prepare_reload().hash, "uid:1000/operator")
    row = db.one(ctx.conn, "SELECT status, invalidated_reason FROM approvals WHERE id = ?", (approval,))
    assert row is not None and row["status"] == "invalidated" and row["invalidated_reason"] == "config_snapshot_changed"


def test_restart_with_changed_files_records_new_snapshot(ctx: AppContext, paths: Paths, clock: FakeClock) -> None:
    _, action = packaged_contribution(ctx)
    approval = ctx.approvals.create(action, tg_chat_id=4242, body_parts_total=1)
    edit_policy(ctx)
    ctx.conn.close()
    second = build_context(paths, TEST_ROLES, clock=clock)
    snap = second.snapshots.load_at_start()
    assert db.current_policy_hash(second.conn) == snap.hash
    row = db.one(second.conn, "SELECT status FROM approvals WHERE id = ?", (approval,))
    assert row is not None and row["status"] == "invalidated"
    last = second.conn.execute(
        "SELECT data_json FROM audit_events WHERE event = 'config_loaded' ORDER BY seq DESC"
    ).fetchone()
    assert json.loads(last[0])["changed"] == ["policy.yaml"]
    second.conn.close()


def test_restart_keeps_dry_run_and_state(ctx: AppContext, paths: Paths, clock: FakeClock) -> None:
    ctx.control.set_dry_run(False, "tg:4242")
    ctx.conn.close()
    second = build_context(paths, TEST_ROLES, clock=clock)
    second.snapshots.load_at_start()
    assert second.control.view().dry_run is False  # only a DB reset turns it back on (I7)
    second.conn.close()
