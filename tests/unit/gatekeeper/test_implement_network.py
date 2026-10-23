"""``implement_network.decide()`` and its routes (ADR-0024, design §4b/§4e; carried criterion C-D17).

Columns (i) and (i') of the shared decision table ``tests/fixtures/implement_network_table.yaml``."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ossbot.common.schemas import State
from ossbot.common.yamlio import safe_load
from ossbot.gatekeeper import implement_network
from ossbot.gatekeeper.context import AppContext
from tests.conftest import FIXTURES, UID_OPERATOR, UID_REQUESTER, UID_SANDBOX, UID_WORKER, insert_contribution
from tests.unit.gatekeeper.api_helpers import client

S = State
TABLE: list[dict[str, str]] = safe_load((FIXTURES / "implement_network_table.yaml").read_text(encoding="utf-8"))["rows"]
STRICT_REPO = "acme/strict-tool"


def strict_repo(ctx: AppContext, repo: str = STRICT_REPO) -> str:
    """A DB-kept repo marked ``egress: strict`` (ossctl targets strict)."""
    ctx.conn.execute(
        "INSERT INTO discovered_targets (repo, source, status, first_seen, updated_at, egress_strict)"
        " VALUES (?, 'owner', 'kept', 't', 't', 1)",
        (repo,),
    )
    return repo


def configure(ctx: AppContext, setting: str, mode: str) -> None:
    default = ctx.config.egress.mode_default
    if ctx.control.egress_mode(default) != mode:
        ctx.control.set_egress_mode(mode, "tg:4242", default, egress=ctx.config.egress)  # type: ignore[arg-type]
    ctx.control.set_implement_network(setting, "tg:4242", default)  # type: ignore[arg-type]


def item(ctx: AppContext, repo: str, *, grant: bool, state: State = S.IMPLEMENTING, **cols: Any) -> str:
    upstream = strict_repo(ctx) if repo == "strict" else "langfuse/langfuse"
    if grant:
        cols.setdefault("network_grant", 1)
        cols.setdefault("network_grant_version", 1)
    cols.setdefault("proposal_version", 1)
    return insert_contribution(ctx, state, upstream=upstream, **cols)


@pytest.mark.parametrize("row", TABLE, ids=[f"{r['setting']}-{r['mode']}-{r['repo']}" for r in TABLE])
@pytest.mark.parametrize("grant", [False, True], ids=["no_grant", "grant"])
def test_decision_table_columns_i_and_i_prime(ctx: AppContext, row: dict[str, str], grant: bool) -> None:
    configure(ctx, row["setting"], row["mode"])
    cid = item(ctx, row["repo"], grant=grant)
    egress, reason = implement_network.decide(ctx, cid)
    assert egress == row["grant" if grant else "no_grant"]
    if egress == "web":
        assert reason == ("auto" if row["setting"] == "auto" else "rerun")


def test_table_is_complete() -> None:
    cells = {(r["setting"], r["mode"], r["repo"]) for r in TABLE}
    assert len(cells) == len(TABLE) == 3 * 4 * 2


def test_reasons_in_check_order(ctx: AppContext) -> None:
    assert implement_network.decide(ctx, "c_0000000000") == ("none", "unknown_cid")
    planned = item(ctx, "normal", grant=True, state=S.PLANNED)
    configure(ctx, "auto", "sandbox_open")
    assert implement_network.decide(ctx, planned) == ("none", "not_implementing")
    cid = item(ctx, "normal", grant=False)
    configure(ctx, "off", "sandbox_open")
    assert implement_network.decide(ctx, cid) == ("none", "setting_off")
    configure(ctx, "on_request", "open")
    assert implement_network.decide(ctx, cid) == ("none", "egress_mode")
    configure(ctx, "on_request", "sandbox_open")
    assert implement_network.decide(ctx, cid) == ("none", "not_requested")
    strict = item(ctx, "strict", grant=True)
    assert implement_network.decide(ctx, strict) == ("none", "strict")


def test_no_target_fails_closed(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(ctx, "auto", "sandbox_open")
    cid = item(ctx, "normal", grant=True)
    monkeypatch.setattr(ctx.discovered, "effective_target", lambda repo: None)
    assert implement_network.decide(ctx, cid) == ("none", "strict")


def test_grant_version_mismatch_is_no_grant(ctx: AppContext) -> None:
    """C-D17: a grant tapped for proposal version 3 does not count for version 4; auto still applies."""
    configure(ctx, "on_request", "sandbox_open")
    cid = item(ctx, "normal", grant=True, network_grant_version=3, proposal_version=4)
    assert implement_network.decide(ctx, cid) == ("none", "not_requested")
    nulled = item(ctx, "normal", grant=True, network_grant_version=None)
    assert implement_network.decide(ctx, nulled) == ("none", "not_requested")
    configure(ctx, "auto", "sandbox_open")
    assert implement_network.decide(ctx, cid) == ("web", "auto")


def _decisions(ctx: AppContext) -> list[tuple[str | None, dict[str, Any]]]:
    rows = ctx.conn.execute("SELECT cid, data_json FROM audit_events WHERE event = 'implement_egress_decided'")
    return [(r["cid"], json.loads(r["data_json"])) for r in rows]


async def test_post_audits_get_does_not(ctx: AppContext) -> None:
    configure(ctx, "auto", "sandbox_open")
    cid = item(ctx, "normal", grant=False)
    path = f"/v1/contributions/{cid}/implement-egress"
    async with client(ctx, UID_WORKER) as c:
        got = await c.get(path)
    assert got.status_code == 200 and got.json() == {"egress": "web", "reason": "auto"}
    assert _decisions(ctx) == []
    async with client(ctx, UID_SANDBOX) as c:
        resp = await c.post(path, json={"run_id": "r_abc-1"})
        assert (await c.get(path)).status_code == 200
        assert (await c.post(path, json={"run_id": "R!"})).status_code == 400
        assert (await c.post(path, json={"run_id": "r1", "egress": "web"})).status_code == 400
        assert (await c.post("/v1/contributions/nope/implement-egress", json={"run_id": "r1"})).status_code == 404
    assert resp.status_code == 200 and resp.json() == {"egress": "web", "reason": "auto"}
    assert _decisions(ctx) == [(cid, {"run_id": "r_abc-1", "egress": "web", "reason": "auto"})]


async def test_roles(ctx: AppContext) -> None:
    cid = item(ctx, "normal", grant=False)
    path = f"/v1/contributions/{cid}/implement-egress"
    for uid in (UID_OPERATOR, UID_WORKER, UID_REQUESTER):
        async with client(ctx, uid) as c:
            assert (await c.post(path, json={"run_id": "r1"})).status_code == 403
    for uid in (UID_OPERATOR, UID_REQUESTER):
        async with client(ctx, uid) as c:
            assert (await c.get(path)).status_code == 403
    assert _decisions(ctx) == []
