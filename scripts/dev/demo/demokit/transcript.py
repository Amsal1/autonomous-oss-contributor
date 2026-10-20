"""The capture transcript (written by ``tests/e2e/demo_capture.py``), validated with ``extra="forbid"``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BotCall(_M):
    """One Bot API call the gatekeeper made (parameters as the fake Bot API received them)."""

    type: Literal["bot"]
    seq: int
    at: str
    method: Annotated[str, Field(pattern=r"^[A-Za-z]{1,64}$")]
    params: dict[str, Any]
    ok: bool
    result: Any = None


class UserMessage(_M):
    """A message the owner typed (from the update the test fed to the bot)."""

    type: Literal["user"]
    seq: int
    at: str
    message_id: int
    thread: int | None = None
    text: Annotated[str, Field(max_length=4096)]


class Mark(_M):
    """A frame: ``show`` (message ids, in order) as the chat stood after event ``upto`` (default: the mark)."""

    type: Literal["mark"]
    seq: int
    at: str
    name: Annotated[str, Field(pattern=r"^[0-9]{2}-[a-z0-9-]{1,40}$")]
    show: list[int]
    pressed: str | None = None
    upto: int | None = None
    draft: int | None = None


type Event = Annotated[BotCall | UserMessage | Mark, Field(discriminator="type")]


class Transcript(_M):
    version: Literal[1]
    events: list[Event]

    def marks(self) -> dict[str, Mark]:
        out: dict[str, Mark] = {}
        for event in self.events:
            if isinstance(event, Mark):
                if event.name in out:
                    raise ValueError(f"duplicate mark {event.name}")
                out[event.name] = event
        return out


def load(path: Path) -> Transcript:
    return Transcript.model_validate(json.loads(path.read_text(encoding="utf-8")))


def parse(text: str) -> Transcript:
    return Transcript.model_validate(json.loads(text))
