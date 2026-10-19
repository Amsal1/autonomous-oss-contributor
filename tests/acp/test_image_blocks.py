"""Image turns (DESIGN §15.6, CR2-7f): data-URL image parts become ACP image blocks when the server
advertises ``promptCapabilities.image``, and the turn uses the vision role."""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

from tests.acp.conftest import FakeRun, client

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode()


def _photo(text: str = "how much protein?") -> list[dict[str, object]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
            ],
        }
    ]


def test_image_becomes_an_acp_block_on_the_vision_role(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(session={"honour_meta_model": True})
    c = client(run, tmp_path)
    c.chat.completions.create(model="kiro-acp", messages=_photo(), timeout=20)
    prompt = run.requests("session/prompt")[0]["params"]["prompt"]
    assert prompt[1] == {"type": "image", "mimeType": "image/png", "data": PNG}
    assert "how much protein?" in prompt[0]["text"]
    assert run.requests("session/new")[0]["params"]["_meta"]["kiro"]["modelId"] == "claude-opus-5.5"  # vision role
    assert c.last_turn.role == "vision" and c.last_turn.images == 1
    c.close()


def test_no_image_capability_sends_text_only(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(init={"protocolVersion": 1, "agentCapabilities": {"promptCapabilities": {"image": False}}})
    c = client(run, tmp_path)
    c.chat.completions.create(model="kiro-acp", messages=_photo(), timeout=20)
    prompt = run.requests("session/prompt")[0]["params"]["prompt"]
    assert len(prompt) == 1
    assert "cannot receive images" in prompt[0]["text"]
    c.close()


def test_only_latest_user_message_and_data_urls(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake()
    c = client(run, tmp_path)
    msgs = [
        *_photo("old"),
        {"role": "assistant", "content": "seen"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "and this?"},
                {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
            ],
        },
    ]
    c.chat.completions.create(model="kiro-acp", messages=msgs, timeout=20)
    prompt = run.requests("session/prompt")[0]["params"]["prompt"]
    assert len(prompt) == 1  # remote URLs are never fetched by the provider; the old photo is history
    assert c.last_turn.role == "chat"
    c.close()
