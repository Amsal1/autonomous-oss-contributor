"""PDF, voice, text files and locations through Telegram (DESIGN §14.12.5 on the item 37 backend)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ossbot.gatekeeper.chat import media
from ossbot.gatekeeper.context import AppContext
from tests.fakes.fake_telegram import media_update
from tests.telegram.chatkit import FakeBackend, deltas, wire
from tests.telegram.tgharness import OWNER, TgRig, make_rig


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


class FakePdf:
    def __init__(self, text: bytes) -> None:
        self.text = text

    async def extract(self, data: bytes, max_bytes: int) -> bytes:
        return self.text


def _pdf_doc(fid: str = "pdf1") -> dict[str, object]:
    return {"file_id": fid, "mime_type": "application/pdf", "file_name": "paper.pdf", "file_size": 8}


async def test_a_short_pdf_is_summarized_from_the_sandbox_text(rig: TgRig) -> None:
    assert rig.ctx.chat is not None
    rig.ctx.chat.pdf = FakePdf(b"Attention is all you need.")  # type: ignore[assignment]
    backend = wire(rig.ctx, FakeBackend(deltas("It introduces the Transformer.")))
    rig.req.file_bytes["pdf1"] = b"%PDF-1.4"
    await rig.send(media_update(OWNER, OWNER, document=_pdf_doc(), caption="summarize"))
    parts = backend.calls[0][0]
    assert parts[0] == {"type": "input_text", "text": "summarize"}
    assert any("Attention is all you need." in p.get("text", "") for p in parts)
    assert "Transformer" in str(rig.sent("sendRichMessage"))


async def test_pdf_without_the_extractor_gets_the_unavailable_reply(rig: TgRig) -> None:
    assert rig.ctx.chat is not None
    rig.ctx.chat.pdf = None
    backend = wire(rig.ctx, FakeBackend())
    rig.req.file_bytes["pdf2"] = b"%PDF-1.4"
    await rig.send(media_update(OWNER, OWNER, document=_pdf_doc("pdf2")))
    assert media.PDF_UNAVAILABLE in rig.last_text() and backend.calls == []


async def test_voice_is_not_supported_yet(rig: TgRig) -> None:
    backend = wire(rig.ctx, FakeBackend())
    await rig.send(media_update(OWNER, OWNER, voice=True))
    assert "Voice isn't supported yet — send text." in rig.last_text()
    assert backend.calls == [] and rig.req.downloads == []


async def test_heic_document_is_sent_back_as_a_photo_hint(rig: TgRig) -> None:
    wire(rig.ctx, FakeBackend())
    rig.req.file_bytes["h1"] = b"\x00\x00\x00\x18ftypheic" + bytes(32)
    doc = {"file_id": "h1", "mime_type": "image/heic", "file_name": "IMG_1.HEIC", "file_size": 40}
    await rig.send(media_update(OWNER, OWNER, document=doc))
    assert media.AS_PHOTO in rig.last_text()


async def test_a_text_file_and_a_location_reach_hermes(rig: TgRig) -> None:
    backend = wire(rig.ctx, FakeBackend(deltas("ok")))
    rig.req.file_bytes["t1"] = b"hello file"
    doc = {"file_id": "t1", "mime_type": "text/plain", "file_name": "notes.txt", "file_size": 10}
    await rig.send(media_update(OWNER, OWNER, document=doc))
    assert any("hello file" in p.get("text", "") for p in backend.calls[0][0])
    await rig.send(media_update(OWNER, OWNER, location=(52.52, 13.405)))
    assert backend.calls[1][0][0]["text"].startswith("📍 location shared: 52.520000, 13.405000")
