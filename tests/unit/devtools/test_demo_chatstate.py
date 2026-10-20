"""demokit.chatstate: replaying the captured Bot API calls as a client would show them."""

from __future__ import annotations

import pytest

from demokit import chatstate
from demokit.transcript import Transcript
from tests.unit.devtools.conftest import Tx, keyboard


def _tx() -> Transcript:
    return Transcript.model_validate(Tx().doc())


def test_send_edit_markup_delete_pin_reactions_and_topics() -> None:
    tx = Tx()
    tx.bot("createForumTopic", {"name": "🚦 Control"}, {"message_thread_id": 30, "name": "🚦 Control"})
    a = tx.send("<b>one</b>", markup=keyboard(["A", "B"]))
    b = tx.send("two")
    tx.bot("editMessageText", {"message_id": a, "text": "<b>one</b> edited"})
    tx.bot("editMessageReplyMarkup", {"message_id": b, "reply_markup": keyboard(["C"])})
    tx.bot("pinChatMessage", {"message_id": a})
    tx.bot("setMessageReaction", {"message_id": b, "reaction": [{"type": "emoji", "emoji": "👀"}]})
    tx.bot("answerCallbackQuery", {"callback_query_id": "x", "text": "toast"})
    tx.bot("sendMessage", {"text": "refused"}, {"description": "x"}, ok=False)
    c = tx.send("gone")
    tx.bot("deleteMessage", {"message_id": c})
    state = chatstate.replay(Transcript.model_validate(tx.doc()))
    assert state.topics == {30: "🚦 Control"}
    assert state.messages[a].text == "<b>one</b> edited" and state.messages[a].markup is None
    assert state.messages[b].labels() == [["C"]] and state.messages[b].reactions == ["👀"]
    assert state.pinned == a and c not in state.messages


def test_rich_edit_and_drafts_replaced_by_the_final_answer() -> None:
    tx = Tx()
    a = tx.send("classic")
    tx.bot("editMessageText", {"message_id": a, "rich_message": {"blocks": [{"type": "paragraph", "text": "r"}]}})
    tx.user("q?", thread=33)
    tx.bot("sendRichMessageDraft", {"draft_id": 7, "message_thread_id": 33,
                                    "rich_message": {"blocks": [{"type": "thinking", "text": "…"}]}})  # fmt: skip
    mid_draft = len(tx.events)
    final = tx.send_rich([{"type": "paragraph", "text": "answer"}], thread=33)
    transcript = Transcript.model_validate(tx.doc())
    during = chatstate.replay(transcript, upto=mid_draft)
    assert 7 in during.drafts and during.drafts[7].draft
    after = chatstate.replay(transcript)
    assert after.drafts == {} and after.messages[final].kind == "rich"
    assert after.messages[a].kind == "rich" and after.messages[a].text == ""


def test_snapshot_orders_the_shown_messages_and_takes_the_pin_of_the_topic() -> None:
    tx = Tx()
    pin = tx.send("📌 dash", thread=30)
    tx.bot("pinChatMessage", {"message_id": pin})
    u = tx.user("/status", thread=30)
    r = tx.send("reply", thread=30, reply_to=u)
    tx.mark("07-status", [u, r])
    other = tx.send("elsewhere", thread=31)
    tx.mark("03-other", [other])
    transcript = Transcript.model_validate(tx.doc())
    marks = transcript.marks()
    frame = chatstate.snapshot(transcript, marks["07-status"])
    assert [m.key for m in frame.messages] == [f"m{u}", f"m{r}"] and frame.pinned is not None
    assert frame.messages[0].outgoing and frame.messages[1].reply_to == u
    assert chatstate.snapshot(transcript, marks["03-other"]).pinned is None  # the pin is in another topic


def test_unknown_methods_and_missing_messages_fail() -> None:
    tx = Tx()
    tx.bot("sendPoll", {"question": "?"})
    with pytest.raises(chatstate.ReplayError, match="sendPoll"):
        chatstate.replay(Transcript.model_validate(tx.doc()))
    tx = Tx()
    tx.bot("editMessageText", {"message_id": 1, "text": "x"})
    with pytest.raises(chatstate.ReplayError, match="unknown message"):
        chatstate.replay(Transcript.model_validate(tx.doc()))
    tx = Tx()
    tx.mark("01-x", [999])
    transcript = Transcript.model_validate(tx.doc())
    with pytest.raises(chatstate.ReplayError, match="not in the chat"):
        chatstate.snapshot(transcript, transcript.marks()["01-x"])


def test_the_transcript_schema_forbids_extra_fields() -> None:
    doc = Tx().doc()
    doc["events"].append({"type": "user", "seq": 1, "at": "x", "message_id": 1, "text": "t", "html": "<b>"})
    with pytest.raises(ValueError, match="html"):
        Transcript.model_validate(doc)
    assert _tx().events == []
