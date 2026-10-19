"""``POST /v1/chat/selftest`` (operator; DESIGN §15.6, PLAN items 37/38) against the fake Hermes API:
the fixture is attached like an owner's photo, the turn runs on ``role:test``, only metadata returns."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

from ossbot.common.control_flags import FlagState
from ossbot.gatekeeper.chat.backend import HermesApiBackend, RelayGate
from ossbot.gatekeeper.chat.hermes_api import HermesClient
from ossbot.gatekeeper.chat.hermes_surface import SurfaceResult
from ossbot.gatekeeper.context import AppContext
from tests.conftest import FIXTURES, UID_OPERATOR, UID_REQUESTER
from tests.fakes.fake_hermes import FakeHermesApi, answer_events
from tests.unit.gatekeeper.api_helpers import client

NOW = datetime(2026, 10, 19, 12, 10, tzinfo=UTC).timestamp()


def _wire(ctx: AppContext, api: FakeHermesApi) -> None:
    assert ctx.chat is not None
    gate = RelayGate(flags=lambda: FlagState(None, None), lifecycle=lambda: None,
                     probe_doc=lambda: {"passed": True, "checked_at": "2026-10-19T12:00:00Z"},
                     surface=lambda t: SurfaceResult("ok"), tier=lambda: "self_improve",
                     on_surface_kill=lambda r: None, now=lambda: NOW)  # fmt: skip
    ctx.chat.service.backend = HermesApiBackend(HermesClient("hk_test", transport=api.transport()), gate)
    dest = ctx.paths.repo / "tests" / "fixtures" / "chat"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "chat" / "fried-chicken-leg.jpg", dest)


async def test_selftest_attaches_the_fixture_and_returns_only_metadata(ctx: AppContext) -> None:
    api = FakeHermesApi(events=answer_events(["About 25-30 g of protein."]))
    _wire(ctx, api)
    async with client(ctx, UID_OPERATOR) as c:
        resp = await c.post("/v1/chat/selftest", json={"fixture": "fried-chicken-leg.jpg", "deliver": False})
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["status"] == "done" and doc["role"] == "test_vision" and doc["answer_chars"] == 25
    assert "answer" not in doc and doc["attachment"]["kind"] == "image"
    assert doc["attachment"]["id"].startswith("a_")
    body = api.requests[0]["body"]
    assert body["model"] == "role:test" and body["provider"] == "kiro-acp"
    kinds = [p["type"] for p in body["input"][0]["content"]]
    assert kinds == ["input_text", "input_image", "input_text"]
    assert doc["attachment"]["id"] in body["input"][0]["content"][2]["text"]


async def test_selftest_is_operator_only(ctx: AppContext) -> None:
    async with client(ctx, UID_REQUESTER) as c:
        assert (await c.post("/v1/chat/selftest", json={})).status_code == 403
