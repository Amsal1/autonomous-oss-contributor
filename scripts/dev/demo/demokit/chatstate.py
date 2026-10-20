"""Replay a transcript into the chat as a Telegram client would show it.

Messages are keyed by message id; a streaming draft by ``draft_id`` (it disappears when the final
message of its thread arrives). Content methods are applied exactly as the Bot API would; any method
this module does not know fails the replay (fail closed: nothing is silently left out of a frame).
``answerCallbackQuery`` toasts and other calls with no visible effect in the chat are ignored by name.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from demokit.transcript import BotCall, Mark, Transcript, UserMessage

SEND = frozenset({"sendMessage", "sendRichMessage"})
DRAFT = frozenset({"sendMessageDraft", "sendRichMessageDraft"})
NO_VISIBLE_EFFECT = frozenset({
    "getMe", "setMyCommands", "deleteMyCommands", "setChatMenuButton", "setMyShortDescription",
    "setMyDescription", "answerCallbackQuery", "sendChatAction", "getFile",
    "unpinChatMessage", "deleteForumTopic", "editForumTopic",
})  # fmt: skip
Kind = Literal["classic", "rich", "user"]


class ReplayError(ValueError):
    pass


@dataclass
class Message:
    key: str  # "m<message_id>" or "d<draft_id>"
    message_id: int | None
    thread: int | None
    kind: Kind
    method: str  # the method that set the current content ("user" for the owner's messages)
    seq: int  # the event that set the current content
    at: str  # when it was first sent
    text: str = ""  # classic: Telegram HTML; user: plain text
    rich: dict[str, Any] | None = None
    markup: dict[str, Any] | None = None
    reply_to: int | None = None
    reactions: list[str] = field(default_factory=list)
    draft: bool = False

    @property
    def outgoing(self) -> bool:
        return self.kind == "user"

    def payload(self) -> dict[str, Any]:
        """The rendered fields, as captured (the faithfulness hash covers exactly these)."""
        return {"method": self.method, "text": self.text, "rich_message": self.rich, "reply_markup": self.markup,
                "thread": self.thread, "draft": self.draft}  # fmt: skip

    def sha256(self) -> str:
        canon = json.dumps(self.payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def labels(self) -> list[list[str]]:
        rows = (self.markup or {}).get("inline_keyboard") or []
        return [[str(b.get("text", "")) for b in row] for row in rows]


@dataclass
class ChatState:
    messages: dict[int, Message] = field(default_factory=dict)
    drafts: dict[int, Message] = field(default_factory=dict)  # draft_id -> draft
    topics: dict[int, str] = field(default_factory=dict)
    pinned: int | None = None


def _markup(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ReplayError("reply_markup is not an object")
    return value if "inline_keyboard" in value else None


def _rich(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or not isinstance(value.get("blocks"), list):
        raise ReplayError("rich_message without blocks")
    return value


def _thread(params: dict[str, Any]) -> int | None:
    value = params.get("message_thread_id")
    return int(value) if value is not None else None


def _reply(params: dict[str, Any]) -> int | None:
    rp = params.get("reply_parameters")
    if isinstance(rp, str):
        rp = json.loads(rp)
    return int(rp["message_id"]) if isinstance(rp, dict) and rp.get("message_id") is not None else None


def _result_id(call: BotCall) -> int:
    if not isinstance(call.result, dict) or "message_id" not in call.result:
        raise ReplayError(f"{call.method} (seq {call.seq}) has no message id in its result")
    return int(call.result["message_id"])


def _apply_bot(state: ChatState, call: BotCall) -> None:
    p, m = call.params, call.method
    if not call.ok or m in NO_VISIBLE_EFFECT:
        return
    if m in SEND:
        mid = _result_id(call)
        thread = _thread(p)
        rich = _rich(p["rich_message"]) if m == "sendRichMessage" else None
        if rich is None and "text" not in p:
            raise ReplayError(f"{m} (seq {call.seq}) without text")
        state.messages[mid] = Message(
            f"m{mid}", mid, thread, "rich" if rich else "classic", m, call.seq, call.at, text=str(p.get("text", "")),
            rich=rich, markup=_markup(p.get("reply_markup")), reply_to=_reply(p),
        )  # fmt: skip
        for draft_id in [d for d, msg in state.drafts.items() if msg.thread == thread]:
            del state.drafts[draft_id]  # the final answer replaces the streaming draft
    elif m == "editMessageText":
        msg = _existing(state, p, call)
        if "rich_message" in p:
            msg.kind, msg.rich, msg.text = "rich", _rich(p["rich_message"]), ""
        elif "text" in p:
            msg.kind, msg.rich, msg.text = "classic", None, str(p["text"])
        else:
            raise ReplayError(f"editMessageText (seq {call.seq}) without content")
        msg.markup = _markup(p.get("reply_markup"))
        msg.method, msg.seq = m, call.seq
    elif m == "editMessageReplyMarkup":
        msg = _existing(state, p, call)
        msg.markup = _markup(p.get("reply_markup"))
        msg.method, msg.seq = m, call.seq
    elif m in DRAFT:
        draft_id = int(p["draft_id"])
        rich = _rich(p["rich_message"]) if m == "sendRichMessageDraft" else None
        state.drafts[draft_id] = Message(
            f"d{draft_id}", None, _thread(p), "rich" if rich else "classic", m, call.seq, call.at,
            text=str(p.get("text") or ""), rich=rich, draft=True,
        )  # fmt: skip
    elif m == "deleteMessage":
        state.messages.pop(int(p["message_id"]), None)
    elif m == "pinChatMessage":
        state.pinned = int(p["message_id"])
    elif m == "setMessageReaction":
        target = state.messages.get(int(p["message_id"]))
        if target is not None:
            reaction = p.get("reaction") or []
            if isinstance(reaction, str):
                reaction = json.loads(reaction)
            target.reactions = [str(r["emoji"]) for r in reaction if r.get("type", "emoji") == "emoji"]
    elif m == "createForumTopic":
        if not isinstance(call.result, dict):
            raise ReplayError("createForumTopic without a result")
        state.topics[int(call.result["message_thread_id"])] = str(p.get("name", ""))
    else:
        raise ReplayError(f"unknown Bot API method {m!r} (seq {call.seq}): teach chatstate before rendering it")


def _existing(state: ChatState, p: dict[str, Any], call: BotCall) -> Message:
    mid = int(p["message_id"])
    if mid not in state.messages:
        raise ReplayError(f"{call.method} (seq {call.seq}) edits unknown message {mid}")
    return state.messages[mid]


def replay(transcript: Transcript, upto: int | None = None) -> ChatState:
    """The chat after every event with ``seq <= upto`` (all events when None)."""
    state = ChatState()
    for event in transcript.events:
        if upto is not None and event.seq > upto:
            break
        if isinstance(event, BotCall):
            _apply_bot(state, event)
        elif isinstance(event, UserMessage):
            state.messages[event.message_id] = Message(
                f"m{event.message_id}", event.message_id, event.thread, "user", "user", event.seq, event.at,
                text=event.text,
            )  # fmt: skip
    return state


@dataclass(frozen=True)
class Frame:
    mark: Mark
    messages: list[Message]
    topic: str
    pinned: Message | None
    state: ChatState


def snapshot(transcript: Transcript, mark: Mark) -> Frame:
    state = replay(transcript, mark.upto if mark.upto is not None else mark.seq)
    shown: list[Message] = []
    for mid in mark.show:
        if mid not in state.messages:
            raise ReplayError(f"mark {mark.name}: message {mid} is not in the chat")
        shown.append(state.messages[mid])
    if mark.draft is not None:
        if mark.draft not in state.drafts:
            raise ReplayError(f"mark {mark.name}: draft {mark.draft} is not streaming")
        shown.append(state.drafts[mark.draft])
    if not shown:
        raise ReplayError(f"mark {mark.name} shows nothing")
    thread = shown[0].thread
    if any(msg.thread != thread for msg in shown):
        raise ReplayError(f"mark {mark.name} mixes threads")
    pinned = state.messages.get(state.pinned) if state.pinned is not None else None
    if pinned is not None and pinned.thread != thread:
        pinned = None  # a pin belongs to the topic its message is in
    topic = state.topics.get(thread, "") if thread is not None else ""
    return Frame(mark, shown, topic, pinned, state)
