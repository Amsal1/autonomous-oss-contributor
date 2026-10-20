"""Every demo page of a transcript, in a fixed order, each checked with ``assert_faithful``."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from demokit import checks, scenes
from demokit.chatstate import snapshot
from demokit.frame import PAGE_SIZE, Caption, Layout, Theme, render_page
from demokit.transcript import Transcript


@dataclass(frozen=True)
class Page:
    name: str
    entry: dict[str, Any]  # its frames.json entry
    html: str


def build(transcript: Transcript) -> list[Page]:
    scenes.check()
    marks = transcript.marks()
    total = len(scenes.SCENES)
    out: list[Page] = []

    def page(name: str, mark: str, theme: Theme, layout: Layout, caption: Caption | None,
             entry: dict[str, Any]) -> None:  # fmt: skip
        if mark not in marks:
            raise ValueError(f"the transcript has no mark {mark!r}")
        frame = snapshot(transcript, marks[mark])
        text = render_page(frame, theme=theme, layout=layout, caption=caption)
        checks.assert_faithful(text, frame)
        width, height = PAGE_SIZE[layout]
        out.append(Page(name, {"page": name, "mark": mark, "theme": theme, "layout": layout, "width": width,
                               "height": height, "payload_sha256": [m.sha256() for m in frame.messages],
                               **entry}, text))  # fmt: skip

    themes: tuple[Theme, ...] = ("light", "dark")
    for i, scene in enumerate(scenes.SCENES):
        caption = Caption(f"{i + 1} / {total}", scene.title, scene.subtitle, i, total)
        for j, shot in enumerate(scene.shots):
            page(f"gif-{scene.id[:2]}-{j}.html", shot.mark, "light", "gif", caption,
                 {"kind": "gif", "scene": scene.id, "hold_s": shot.hold_s})  # fmt: skip
        for theme in themes:
            page(f"still-{scene.id}-{theme}.html", scene.still, theme, "still", None,
                 {"kind": "still", "scene": scene.id, "still": f"{scene.id}-{theme}.png"})  # fmt: skip
    return out


def digest(built: list[Page]) -> str:
    """The render digest: changes whenever any page would change."""
    h = hashlib.sha256()
    for p in built:
        h.update(p.name.encode() + b"\0" + p.html.encode("utf-8") + b"\0")
    return h.hexdigest()
