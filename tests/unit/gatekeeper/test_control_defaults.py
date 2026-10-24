"""A fresh install stays conservative (build-env AC 1, BE-6.1, ADR-0025): dry-run on, implement network off,
egress mode ``open`` (the snapshot's ``mode_default``; no repo-code pool gets web_public there)."""

from __future__ import annotations

from ossbot.common.control_flags import CODE_WEB_MODES, EGRESS_MODE_FILE
from ossbot.gatekeeper.context import AppContext


def test_fresh_db_defaults(ctx: AppContext) -> None:
    assert not (ctx.paths.control_dir / EGRESS_MODE_FILE).exists()
    assert ctx.control.view().dry_run is True
    assert ctx.control.implement_network() == "off"
    default = ctx.config.egress.mode_default
    assert default == "open"
    assert ctx.control.egress_mode(default) == "open"
    assert default not in CODE_WEB_MODES
    assert not ctx.config.egress.repo_code_web(default)


def test_sandbox_full_is_never_the_default(ctx: AppContext) -> None:
    """The fifth mode exists in the shipped config but only the owner selects it."""
    assert "sandbox_full" in ctx.config.egress.modes
    assert ctx.config.egress.mode_default != "sandbox_full"
