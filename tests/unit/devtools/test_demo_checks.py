"""demokit.checks.assert_faithful: a page passes only when it shows exactly the captured payloads."""

from __future__ import annotations

import pytest

from demokit import chatstate, checks, frame
from demokit.transcript import Transcript
from tests.unit.devtools.conftest import Tx, keyboard


def _frame() -> chatstate.Frame:
    tx = Tx()
    pin = tx.send("🟢 <b>ossbot</b> · UP\nmore", thread=30)
    tx.bot("pinChatMessage", {"message_id": pin})
    u = tx.user("/status", thread=30)
    reply = tx.send("Dry run <i>done</i>", thread=30, markup=keyboard(["🧪 Confirm dry run", "↩️ Cancel"]),
                    reply_to=u)  # fmt: skip
    tx.mark("05-panel", [u, reply])
    transcript = Transcript.model_validate(tx.doc())
    return chatstate.snapshot(transcript, transcript.marks()["05-panel"])


def _page() -> str:
    return frame.render_page(_frame(), theme="light", layout="still")


def test_the_rendered_page_passes() -> None:
    assert checks.assert_faithful(_page(), _frame()) == 2


@pytest.mark.parametrize(("old", "new", "why"), [
    ("Dry run <i>done</i>", "Dry run <i>DONE</i>", "text differs"),
    ("🧪 Confirm dry run</div>", "🚀 Confirm LIVE</div>", "buttons"),
    ('<div class="inputbar" data-chrome="input">', '<div class="inputbar">', "outside data-chrome"),
    ("</main>", "<p>Approved by the owner</p></main>", "outside data-chrome"),
    ('<span class="pin-text">🟢 ossbot · UP', '<span class="pin-text">🛑 KILLED', "pinned bar"),
    ('data-chrome="reply" data-ref="m500">/status', 'data-chrome="reply" data-ref="m500">/kill', "reply quote"),
])  # fmt: skip
def test_tampering_is_caught(old: str, new: str, why: str) -> None:
    page = _page()
    assert old in page
    with pytest.raises(checks.Unfaithful, match=why):
        checks.assert_faithful(page.replace(old, new, 1), _frame())


def test_a_wrong_hash_or_a_missing_message_is_caught() -> None:
    page = _page()
    sha = _frame().messages[1].sha256()
    with pytest.raises(checks.Unfaithful, match="hash"):
        checks.assert_faithful(page.replace(sha, "0" * 64), _frame())
    start = page.index('<div class="msg out"')
    end = page.index('<div class="msg in"')
    with pytest.raises(checks.Unfaithful, match="page shows"):
        checks.assert_faithful(page[:start] + page[end:], _frame())
