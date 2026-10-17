"""Monotonic personal floors (DESIGN §7.9): reloads can add but never silently remove."""

from __future__ import annotations

import pytest

from ossbot.common.clock import FakeClock
from ossbot.common.paths import Paths
from ossbot.gatekeeper import db, policy
from ossbot.gatekeeper.context import AppContext, build_context
from ossbot.gatekeeper.policy import PolicyStartupError
from tests.conftest import TEST_ROLES


def reload(ctx: AppContext) -> None:
    preview = ctx.snapshots.prepare_reload()
    ctx.snapshots.apply_reload(preview.hash, "uid:1000/operator")


def edit_profile(ctx: AppContext, old: str, new: str) -> None:
    path = ctx.paths.config_dir / "profile.yaml"
    path.write_text(path.read_text().replace(old, new))


def test_profile_values_land_in_the_floor(ctx: AppContext) -> None:
    assert policy.floor_values(ctx.conn, "forbidden_login") == {"test-work-login"}
    assert policy.floor_values(ctx.conn, "confidentiality") == {"test-work-login", "@corp.example"}


def test_reload_dropping_a_value_keeps_it(ctx: AppContext) -> None:
    edit_profile(
        ctx, 'confidentiality_floor: [test-work-login, "@corp.example"]', "confidentiality_floor: [test-work-login]"
    )
    reload(ctx)
    conf = policy.effective_confidentiality(ctx.conn, ctx.config)
    assert "@corp.example" in conf
    assert "confidential" in {v.code for v in policy.check_text("comment", "a@corp.example", ctx.config, conf)}


def test_reload_adding_a_value_extends_the_floor(ctx: AppContext) -> None:
    edit_profile(ctx, "forbidden_logins: [test-work-login]", "forbidden_logins: [test-work-login, second-work]")
    reload(ctx)
    assert policy.effective_forbidden_logins(ctx.conn, ctx.config.profile) == {"test-work-login", "second-work"}


def test_removal_needs_explicit_path(ctx: AppContext, clock: FakeClock) -> None:
    edit_profile(ctx, "forbidden_logins: [test-work-login]", "forbidden_logins: [other-login]")
    reload(ctx)
    assert "test-work-login" in policy.effective_forbidden_logins(ctx.conn, ctx.config.profile)
    with db.transaction(ctx.conn):
        assert policy.remove_floor(ctx.conn, clock, "forbidden_login", "test-work-login", "uid:1000/operator+tg:4242")
    assert "test-work-login" not in policy.effective_forbidden_logins(ctx.conn, ctx.config.profile)


def test_fresh_db_with_empty_forbidden_logins_refuses_to_start(paths: Paths, clock: FakeClock) -> None:
    profile = paths.config_dir / "profile.yaml"
    profile.write_text(profile.read_text().replace("forbidden_logins: [test-work-login]", "forbidden_logins: []"))
    ctx = build_context(paths, TEST_ROLES, clock=clock)
    with pytest.raises(PolicyStartupError):
        ctx.snapshots.load_at_start()
    assert db.read_control_row(ctx.conn) is None  # nothing committed


def test_existing_floor_allows_empty_profile_list(ctx: AppContext) -> None:
    edit_profile(ctx, "forbidden_logins: [test-work-login]", "forbidden_logins: []")
    reload(ctx)  # the floor still holds test-work-login, so the reload is accepted
    assert policy.effective_forbidden_logins(ctx.conn, ctx.config.profile) == {"test-work-login"}


def test_missing_profile_refuses_to_start(paths: Paths, clock: FakeClock) -> None:
    from ossbot.common.config import ConfigError

    (paths.config_dir / "profile.yaml").unlink()
    ctx = build_context(paths, TEST_ROLES, clock=clock)
    with pytest.raises(ConfigError, match=r"profile\.yaml"):
        ctx.snapshots.load_at_start()
