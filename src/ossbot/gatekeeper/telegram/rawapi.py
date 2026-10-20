"""Typed Bot API 10.1-10.3 adapter over ``Bot.do_api_request`` (ADR-0020, DESIGN §14.20.4).

python-telegram-bot 22.8 speaks Bot API 10.0; everything newer goes through raw calls with one
pydantic request model per method (``extra="forbid"``: a misspelt or undocumented field fails in a
test, never silently on the wire) and one response model per result we read (``extra="ignore"``: a
new Telegram field can never break parsing). Every outbox send already uses ``do_api_request``
(``bot.TelegramSender``), so these payloads travel through the outbox unchanged. Field lists were
checked against the live reference on 2026-10-19 (ADR-0020 addendum; ``tests/telegram/test_rawapi.py``).

Incoming 10.3 shapes (``stopped_message_generation``) arrive in ``Update.api_kwargs`` (PTB keeps
unknown keys there instead of raising) and are parsed with ``MessageGenerationStopped``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ossbot.gatekeeper.telegram.ui.blocks import InputRichMessage

Style = Literal["danger", "success", "primary"]
CALLBACK_RE = r"^t:[A-Za-z0-9_-]{22}$"


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class _Resp(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# ------------------------------------------------------------------------------- reply markup
class CopyTextButton(_Req):
    text: Annotated[str, Field(min_length=1, max_length=256)]


class DisabledButton(_Req):
    """Bot API 10.3: holds no information."""


class InlineKeyboardButton(_Req):
    text: Annotated[str, Field(min_length=1)]
    style: Style | None = None
    callback_data: Annotated[str, Field(pattern=CALLBACK_RE)] | None = None
    url: Annotated[str, Field(pattern=r"^https://", max_length=2048)] | None = None
    copy_text: CopyTextButton | None = None
    disabled: DisabledButton | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> InlineKeyboardButton:
        kinds = [k for k in ("callback_data", "url", "copy_text", "disabled") if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError(f"a button needs exactly one action, got {kinds}")
        return self


class InlineKeyboardMarkup(_Req):
    inline_keyboard: list[list[InlineKeyboardButton]]
    force_reply: Literal[True] | None = None  # Bot API 10.3; can't change when the keyboard is edited


class ForceReply(_Req):
    force_reply: Literal[True] = True
    input_field_placeholder: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    selective: bool | None = None


class ReplyParameters(_Req):
    message_id: int
    allow_sending_without_reply: Literal[True] | None = True


# ------------------------------------------------------------------------------- methods
class SendRichMessage(_Req):
    chat_id: int
    message_thread_id: int | None = None
    rich_message: InputRichMessage
    disable_notification: bool | None = None
    protect_content: bool | None = None
    message_effect_id: str | None = None
    reply_parameters: ReplyParameters | None = None
    reply_markup: InlineKeyboardMarkup | ForceReply | None = None


class EditMessageRich(_Req):
    """``editMessageText`` with ``rich_message`` (Bot API 10.1)."""

    chat_id: int
    message_id: int
    rich_message: InputRichMessage
    reply_markup: InlineKeyboardMarkup | None = None


class SendRichMessageDraft(_Req):
    chat_id: int
    message_thread_id: int | None = None
    draft_id: Annotated[int, Field(ge=1)]
    rich_message: InputRichMessage
    can_stop: bool | None = None
    keep_on_stop: bool | None = None


class SendMessageDraft(_Req):
    """``sendMessageDraft`` with the 10.3 ``can_stop``/``keep_on_stop`` (PTB has the method without them)."""

    chat_id: int
    message_thread_id: int | None = None
    draft_id: Annotated[int, Field(ge=1)]
    text: Annotated[str, Field(max_length=4096)] | None = None
    parse_mode: Literal["HTML"] | None = None
    can_stop: bool | None = None
    keep_on_stop: bool | None = None


class DeleteForumTopic(_Req):
    """``deleteForumTopic``: in a private chat with a user since Bot API 9.3, no rights needed (only a
    supergroup needs ``can_delete_messages``); checked 2026-10-21 (ADR-0020 addendum). There is no
    private-chat close or hide: ``closeForumTopic`` and ``hideGeneralForumTopic`` are supergroup-only."""

    chat_id: int
    message_thread_id: Annotated[int, Field(ge=1)]


METHODS: dict[str, type[_Req]] = {
    "sendRichMessage": SendRichMessage,
    "editMessageText#rich": EditMessageRich,
    "sendRichMessageDraft": SendRichMessageDraft,
    "sendMessageDraft": SendMessageDraft,
    "deleteForumTopic": DeleteForumTopic,
}


def method_name(key: str) -> str:
    return key.split("#", 1)[0]


# ------------------------------------------------------------------------------- results
class MeResult(_Resp):
    """``getMe``: the 9.3/9.4 topic fields are read here (present only in getMe)."""

    id: int
    username: str | None = None
    has_topics_enabled: bool = False
    allows_users_to_create_topics: bool = False


class ForumTopicResult(_Resp):
    message_thread_id: int
    name: str


class SentMessage(_Resp):
    message_id: int


class ChatRef(_Resp):
    id: int
    type: str


class MessageGenerationStopped(_Resp):
    """Incoming ``stopped_message_generation`` (Bot API 10.3), from ``Update.api_kwargs``. No ``from``:
    authorised by ``auth.authorized_chat_only`` (U10)."""

    chat: ChatRef
    message_thread_id: int | None = None
    draft_id: int


def parse_stopped(api_kwargs: Mapping[str, Any] | Any) -> MessageGenerationStopped | None:
    """PTB keeps unknown update keys in ``api_kwargs``, a read-only ``MappingProxyType`` (not a dict)."""
    raw = api_kwargs.get("stopped_message_generation") if isinstance(api_kwargs, Mapping) else None
    if not isinstance(raw, dict):
        return None
    try:
        return MessageGenerationStopped.model_validate(raw)
    except ValueError:
        return None
