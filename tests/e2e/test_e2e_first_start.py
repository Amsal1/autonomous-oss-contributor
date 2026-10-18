"""M11 end to end (DESIGN §10.9): empty catalog and empty probe table -> list_models, catalog upsert,
POST /v1/probes/run with resolved models, then the first stage, in that order. A list_models
failure -> catalog_empty alert, no probe and no Kiro job."""

from __future__ import annotations

from tests.e2e.conftest import E2E


def test_first_start_order(e2e: E2E) -> None:
    e2e.ctx.conn.execute("DELETE FROM model_catalog")
    cid = e2e.discover_one()
    e2e.run_until(cid, "VETTED")
    assert e2e.log[:2] == ["list_models", "kiro:vetter"]
    assert e2e.probes.runs == [{"planner": "claude-opus-5.5", "coder": "claude-opus-5.5"}]
    assert e2e.ctx.conn.execute("SELECT COUNT(*) FROM model_catalog").fetchone()[0] == 5


def test_list_models_failure_alerts_and_starts_nothing(e2e: E2E) -> None:
    e2e.ctx.conn.execute("DELETE FROM model_catalog")
    e2e.runtime.handlers["list_models"] = lambda rt, c: 2
    cid = e2e.discover_one()
    for _ in range(3):
        assert e2e.ready_scheduler().tick().skipped == "not_ready"
    assert e2e.log == [] and e2e.probes.runs == [] and e2e.state(cid) == "DISCOVERED"
    assert sum("catalog_empty" in s for s in e2e.said) == 1
