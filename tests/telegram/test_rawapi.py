"""ADR-0020: the typed Bot API 10.1-10.3 adapter. Every request model is checked against the documented
field list (core.telegram.org/bots/api, checked 2026-10-19; ADR-0020 addendum): a field we send that the
API does not document fails here, and so does a misspelling (``extra="forbid"``)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ossbot.gatekeeper.telegram import rawapi

# Documented parameters per method (Bot API 10.3) that ossbot may send; the models must not exceed them.
DOCUMENTED: dict[type[rawapi._Req], set[str]] = {
    rawapi.SendRichMessage: {"business_connection_id", "chat_id", "message_thread_id", "direct_messages_topic_id",
                             "ephemeral_message_parameters", "rich_message", "disable_notification",
                             "protect_content", "allow_paid_broadcast", "message_effect_id",
                             "suggested_post_parameters", "reply_parameters", "reply_markup"},
    rawapi.SendRichMessageDraft: {"chat_id", "message_thread_id", "draft_id", "rich_message", "can_stop",
                                  "keep_on_stop"},
    rawapi.SendMessageDraft: {"chat_id", "message_thread_id", "draft_id", "text", "parse_mode", "entities",
                              "can_stop", "keep_on_stop"},
    rawapi.EditMessageRich: {"business_connection_id", "chat_id", "message_id", "inline_message_id", "text",
                             "parse_mode", "entities", "link_preview_options", "rich_message", "reply_markup"},
    # the documented fields ossbot may use (button types it never uses are left out of this set)
    rawapi.InlineKeyboardButton: {"text", "icon_custom_emoji_id", "style", "url", "callback_data",
                                  "switch_inline_query", "switch_inline_query_current_chat",
                                  "switch_inline_query_chosen_chat", "copy_text", "callback_game", "pay",
                                  "disabled"},
    rawapi.InlineKeyboardMarkup: {"inline_keyboard", "force_reply"},
    rawapi.ForceReply: {"force_reply", "input_field_placeholder", "selective"},
    rawapi.CopyTextButton: {"text"},
    rawapi.DisabledButton: set(),
}  # fmt: skip
RICH = {"blocks": [{"type": "paragraph", "text": "hello"}]}


@pytest.mark.parametrize("model", list(DOCUMENTED))
def test_request_models_only_use_documented_fields(model: type[rawapi._Req]) -> None:
    assert set(model.model_fields) <= DOCUMENTED[model], set(model.model_fields) - DOCUMENTED[model]


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        rawapi.SendRichMessage.model_validate({"chat_id": 1, "rich_message": RICH, "parse_mode": "HTML"})
    with pytest.raises(ValidationError):
        rawapi.SendMessageDraft.model_validate({"chat_id": 1, "draft_id": 0})  # must be non-zero


def test_payloads_drop_unset_fields() -> None:
    req = rawapi.SendRichMessageDraft(chat_id=1, draft_id=7, rich_message=RICH, can_stop=True)  # type: ignore[arg-type]
    assert req.payload() == {"chat_id": 1, "draft_id": 7, "can_stop": True,
                             "rich_message": {"blocks": [{"type": "paragraph", "text": "hello"}],
                                              "skip_entity_detection": True}}  # fmt: skip


def test_button_needs_exactly_one_action() -> None:
    rawapi.InlineKeyboardButton(text="x", callback_data="t:" + "A" * 22)
    rawapi.InlineKeyboardButton(text="x", disabled=rawapi.DisabledButton())
    rawapi.InlineKeyboardButton(text="x", copy_text=rawapi.CopyTextButton(text="title"), style="primary")
    with pytest.raises(ValidationError):
        rawapi.InlineKeyboardButton(text="x")
    with pytest.raises(ValidationError):
        rawapi.InlineKeyboardButton(text="x", callback_data="t:" + "A" * 22, url="https://github.com")
    with pytest.raises(ValidationError):
        rawapi.InlineKeyboardButton(text="x", callback_data="approve")  # only gatekeeper tokens (U2)
    with pytest.raises(ValidationError):
        rawapi.InlineKeyboardButton(text="x", url="http://plain.example")  # https only


def test_responses_ignore_new_fields() -> None:
    me = rawapi.MeResult.model_validate({"id": 1, "is_bot": True, "first_name": "b", "has_topics_enabled": True,
                                         "a_field_from_bot_api_11": {"x": 1}})  # fmt: skip
    assert me.has_topics_enabled and not me.allows_users_to_create_topics


def test_stopped_message_generation_from_api_kwargs() -> None:
    stopped = rawapi.parse_stopped({"stopped_message_generation": {"chat": {"id": 4242, "type": "private"},
                                                                   "draft_id": 99, "future": 1}})  # fmt: skip
    assert stopped is not None and stopped.draft_id == 99 and stopped.chat.id == 4242
    assert rawapi.parse_stopped({}) is None
    assert rawapi.parse_stopped({"stopped_message_generation": {"draft_id": "x"}}) is None
