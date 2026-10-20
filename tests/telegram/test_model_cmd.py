"""``/model`` and the new-model card (DESIGN §14.14.1, §16.3; AC 13, CHAT-12): role picker → catalog pager
(8 per page, the current one ✅) → effort → confirm → ``models.yaml`` written by the existing writer."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.common.schemas import CatalogEntry
from ossbot.gatekeeper.context import AppContext
from tests.telegram.reviewkit import labels, sent_with, text, token
from tests.telegram.tgharness import TgRig, make_rig

IDS = [f"model-{n:02d}" for n in range(10)]


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    ctx.models.upsert_catalog([CatalogEntry(model_id=i) for i in IDS], "uid:2001/worker")
    async for r in make_rig(ctx):
        yield r


async def test_role_catalog_effort_confirm(rig: TgRig) -> None:
    await rig.say("/model")
    mid = sent_with(rig, "Which role?")
    assert labels(rig, mid) == ["chat", "vision", "planner", "coder", "reviewer", "summarizer"]
    await rig.tap(token(rig, mid, "coder"), message=mid)
    assert len([lbl for lbl in labels(rig, mid) if lbl.startswith("model-")]) == 8  # 8 per page
    assert "1/2" in labels(rig, mid)
    await rig.tap(token(rig, mid, "▶"), message=mid)
    assert [lbl for lbl in labels(rig, mid) if lbl.startswith("model-")] == ["model-08", "model-09"]
    await rig.tap(token(rig, mid, "model-09"), message=mid)
    assert labels(rig, mid)[:5] == ["low", "medium", "high", "xhigh", "max"]
    await rig.tap(token(rig, mid, "medium"), message=mid)
    assert "model-09 (medium)" in text(rig, mid)
    assert rig.ctx.models.read().roles.coder.model != "model-09"  # nothing before the confirm
    await rig.tap(token(rig, mid, "✅ Switch model"), message=mid)
    roles = rig.ctx.models.read().roles
    assert roles.coder.model == "model-09" and roles.coder.effort == "medium"
    assert "coder → model-09" in text(rig, mid)


async def test_current_model_is_marked(rig: TgRig) -> None:
    rig.ctx.models.set_model("chat", "model-01", None, False, "uid:1000/operator")
    await rig.say("/model chat")
    mid = sent_with(rig, "Model for chat")
    assert "✅ model-01" in labels(rig, mid)


async def test_new_model_card_and_ignore(rig: TgRig) -> None:
    rig.ctx.models.upsert_catalog([CatalogEntry(model_id="model-new", name="Shiny")], "uid:2001/worker")
    await rig.flush()
    mid = sent_with(rig, "New Kiro model")
    assert "model-new" in text(rig, mid) and "Shiny" in text(rig, mid)
    assert labels(rig, mid) == ["Set as default", "Set for role…", "Ignore"]
    await rig.tap(token(rig, mid, "Set as default"), message=mid)
    assert "Model: default" in text(rig, mid) and labels(rig, mid) == ["✅ Switch model", "↩️ Cancel"]
    await rig.tap(token(rig, mid, "↩️ Cancel"), message=mid)
    assert rig.ctx.models.read().default.model != "model-new"


async def test_new_model_card_for_a_role(rig: TgRig) -> None:
    rig.ctx.models.upsert_catalog([CatalogEntry(model_id="model-new")], "uid:2001/worker")
    await rig.flush()
    mid = sent_with(rig, "New Kiro model")
    await rig.tap(token(rig, mid, "Set for role…"), message=mid)
    assert labels(rig, mid)[0] == "default" and "reviewer" in labels(rig, mid)
    await rig.tap(token(rig, mid, "reviewer"), message=mid)
    await rig.tap(token(rig, mid, "Keep effort"), message=mid)
    await rig.tap(token(rig, mid, "✅ Switch model"), message=mid)
    assert rig.ctx.models.read().roles.reviewer.model == "model-new"
