"""``ossctl backup`` around the archive (PLAN item 43): it lifts only its own pause, and only through
``ossbot-resume --paused-only``; the gatekeeper records each archive (metadata only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ossbot.ctl import cli
from ossbot.gatekeeper.context import AppContext
from tests.conftest import UID_OPERATOR, UID_WORKER, write_flag
from tests.unit.gatekeeper.api_helpers import client


@pytest.fixture
def helper_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake(name: str, *args: str, quiet: bool = False) -> int:
        calls.append((name, *args))
        return 0

    monkeypatch.setattr(cli, "_root_helper", fake)
    return calls


def _seq(ctx: AppContext) -> int:
    return int(ctx.conn.execute("SELECT MAX(seq) FROM audit_events").fetchone()[0])


def test_resumes_its_own_pause_with_paused_only(ctx: AppContext, helper_calls: list[tuple[str, ...]]) -> None:
    ctx.control.pause(cli.BACKUP_PAUSE_REASON, "uid:0/operator")
    assert cli._backup_resume(ctx.paths, _seq(ctx)) == "resumed"
    assert helper_calls == [
        ("ossbot-resume", "--offline", "--via", "backup", "--actor", "ossctl-backup", "--paused-only")
    ]


@pytest.mark.parametrize("case", ["killed", "someone_paused", "other_reason", "resumed"])
def test_leaves_any_other_pause_or_kill_alone(ctx: AppContext, helper_calls: list[tuple[str, ...]], case: str) -> None:
    ctx.control.pause(cli.BACKUP_PAUSE_REASON if case != "other_reason" else "owner", "uid:0/operator")
    seq = _seq(ctx)
    if case == "killed":
        write_flag(ctx.paths.killed_flag, {"level": "soft", "reason": "tg", "actor": "tg:1"})
    elif case == "someone_paused":
        ctx.control.pause("owner wants it paused", "tg:4242")
    elif case == "resumed":
        ctx.paths.paused_flag.unlink()
    out = cli._backup_resume(ctx.paths, seq)
    assert helper_calls == [] and out.startswith(("left", "already"))


async def test_backup_record_is_audited_operator_only(ctx: AppContext) -> None:
    body = {"archive": "ossbot-20261019-120000.tar.age", "sha256": "a" * 64, "size": 10, "items": 3,
            "includes_secrets": False, "includes_artifacts": False, "includes_attachments": False}  # fmt: skip
    async with client(ctx, UID_WORKER) as c:
        assert (await c.post("/v1/audit/backup-created", json=body)).status_code == 403
    async with client(ctx, UID_OPERATOR) as c:
        assert (await c.post("/v1/audit/backup-created", json={**body, "archive": "../x"})).status_code == 400
        resp = await c.post("/v1/audit/backup-created", json=body)
    assert resp.status_code == 200 and resp.json()["recorded"] is True
    data = ctx.conn.execute("SELECT data_json FROM audit_events WHERE event = 'backup_created'").fetchone()[0]
    assert json.loads(data) == body


def test_backup_timer_is_installed_but_never_enabled_by_provisioning() -> None:
    root = Path(cli.__file__).resolve().parents[3]
    services_sh = (root / "scripts" / "lib" / "services.sh").read_text(encoding="utf-8")
    assert "ossbot-backup" not in services_sh  # neither enabled nor started; `ossctl backup schedule on`
    unit = (root / "systemd" / "ossbot-backup.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/ossbot/app/.venv/bin/ossctl backup now" in unit and "User=" not in unit
    assert "OnCalendar=" in (root / "systemd" / "ossbot-backup.timer").read_text(encoding="utf-8")
