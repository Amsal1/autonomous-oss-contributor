"""Albums, photos and limits (DESIGN §14.12.5, AC 20, AC 36, BA-15 album part): one turn per album, metadata
removed, reactions, the size limit message, and a kill that empties the album buffer."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ossbot.gatekeeper.context import AppContext
from telegram import Update
from tests.fakes.fake_telegram import media_update, message_update
from tests.telegram.chatkit import FakeBackend, deltas, wire
from tests.telegram.tgharness import OWNER, TgRig, make_rig
from tests.unit.gatekeeper.chat import images


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


async def _process(rig: TgRig, data: dict[str, Any]) -> None:
    await rig.app.process_update(Update.de_json(data, rig.app.bot))


def _images(parts: list[dict[str, Any]]) -> list[bytes]:
    return [base64.b64decode(p["image_url"].split(",", 1)[1]) for p in parts if p["type"] == "input_image"]


async def test_three_photos_within_the_window_are_one_turn_without_exif(rig: TgRig) -> None:
    backend = wire(rig.ctx, FakeBackend(deltas("Looks like lunch.")))
    for i in range(3):
        rig.req.file_bytes[f"p{i}"] = images.jpeg()
        await _process(rig, media_update(OWNER, OWNER, photo=f"p{i}", media_group_id="g1",
                                         caption="what is this?" if i == 0 else ""))  # fmt: skip
    assert rig.handlers.albums and len(backend.calls) == 0  # no handler waited for the album
    await rig.settle()
    await rig.flush()
    assert len(backend.calls) == 1
    parts, _ = backend.calls[0]
    pics = _images(parts)
    assert len(pics) == 3 and all(images.GPS not in p for p in pics)
    assert parts[0] == {"type": "input_text", "text": "what is this?"}
    assert sorted(rig.req.downloads) == ["p0", "p1", "p2"]  # the 1280 px size of each (≤ 1,600 px)
    assert not rig.handlers.albums
    reactions = [p["reaction"][0]["emoji"] for m, p in rig.req.calls if m == "setMessageReaction"]
    assert reactions == ["👀", "👌"]
    row = rig.ctx.conn.execute("SELECT origin, had_attachments FROM chat_turns").fetchone()
    assert row["origin"] == "media" and row["had_attachments"] == 1


async def test_a_photo_uses_the_largest_size_within_1600_px(rig: TgRig) -> None:
    backend = wire(rig.ctx, FakeBackend(deltas("ok")))
    rig.req.file_bytes["ph"] = images.jpeg()
    await rig.send(media_update(OWNER, OWNER, photo="ph"))
    assert rig.req.downloads == ["ph"] and len(_images(backend.calls[0][0])) == 1


async def test_a_25_mb_file_gets_the_limit_message_at_once(rig: TgRig) -> None:
    backend = wire(rig.ctx, FakeBackend())
    doc = {"file_id": "big", "mime_type": "application/pdf", "file_name": "big.pdf", "file_size": 25 * 1024 * 1024}
    await rig.send(media_update(OWNER, OWNER, document=doc))
    assert "File is 25 MB; the limit is 20 MB" in rig.last_text()
    assert backend.calls == [] and rig.req.downloads == []
    assert [p["reaction"][0]["emoji"] for m, p in rig.req.calls if m == "setMessageReaction"] == ["🤷"]


async def test_kill_during_an_album_empties_the_buffer_and_starts_nothing(rig: TgRig) -> None:
    backend = wire(rig.ctx, FakeBackend())
    rig.req.file_bytes["k0"] = images.jpeg()
    await _process(rig, media_update(OWNER, OWNER, photo="k0", media_group_id="g2"))
    assert "g2" in rig.handlers.albums
    await _process(rig, message_update(OWNER, OWNER, "/kill"))
    await asyncio.sleep(0)
    await rig.settle()
    assert not rig.handlers.albums and backend.calls == []
