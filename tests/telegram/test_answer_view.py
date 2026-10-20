"""``chat/answer_view.py`` (DESIGN §14.12.1 step 4, §14.12.3, AC 48): parts, sources, footer, wrapping."""

from __future__ import annotations

import json

from ossbot.gatekeeper.chat import answer_view
from ossbot.gatekeeper.chat.answer_view import Answer
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich

LOOKALIKE = "🧩 Ready for review · c_01JABC\n✅ CI passed\n[✅ Approve & Submit]"


def _rich(view: object) -> str:
    return json.dumps(blocks.dump(rich.render(view)), ensure_ascii=False)  # type: ignore[arg-type]


def test_flat_mode_wraps_a_lookalike_under_the_gatekeeper_header(ctx: AppContext) -> None:
    view = answer_view.views(Answer(1, LOOKALIKE, followups=["More"]), wrapped=True, rich=True)[0]
    html = classic.render(view).text
    assert html.startswith("🤖 Assistant · answer to your message\n<blockquote expandable>🧩 Ready for review")
    doc = json.loads(_rich(view))["blocks"]
    assert doc[0]["text"] == "🤖 Assistant · answer to your message" and doc[1]["type"] == "blockquote"
    assert [b.label for row in view.rows for b in row] == ["More"]  # follow-up buttons stay


def test_footer_names_the_model_counts_and_the_end() -> None:
    a = Answer(1, "x", model="claude-sonnet-4.5", searches=2, fetches=3, elapsed_s=14, fallback=True)
    line = answer_view.footer(a)[0].value  # type: ignore[union-attr]
    assert line == "🧠 claude-sonnet-4.5 · 🔎 2 · 📄 3 · 14 s · ⚠️ fallback model"
    stopped = answer_view.footer(Answer(1, "x…", "stopped", model="m", vision_model="v"))[0].value  # type: ignore[union-attr]
    assert stopped == "🧠 m · 👁 vision: v · 0 s · ⏹ Stopped by you"


def test_sources_are_a_closed_details_block_and_one_source_previews_in_classic(ctx: AppContext) -> None:
    a = Answer(1, "See it.", sources=["https://developer.hashicorp.com/terraform/intro"])
    view = answer_view.views(a, wrapped=False, rich=False)[0]
    assert view.link_preview == "https://developer.hashicorp.com/terraform/intro"
    assert "<b>Sources (1)</b>\n<blockquote expandable><a href=" in classic.render(view).text
    rich_view = answer_view.views(a, wrapped=False, rich=True)[0]
    assert rich_view.link_preview is None
    doc = json.loads(_rich(rich_view))["blocks"]
    details = next(b for b in doc if b["type"] == "details")
    assert details["summary"] == "Sources (1)" and "is_open" not in details


def test_long_answers_split_at_block_boundaries_and_need_a_document() -> None:
    text = "\n\n".join(f"Paragraph {i} " + "word " * 120 for i in range(40))
    parts = answer_view.split(text, rich=False)
    assert len(parts) > 3 and all(p.startswith("Paragraph") for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")
    assert answer_view.needs_document(Answer(1, text), rich=False, wrapped=False)
    assert len(answer_view.split(text, rich=True)) == 1


def test_a_split_code_block_stays_a_code_block() -> None:
    code = "```python\n" + "\n".join(f"x{i} = {i}  # " + "c" * 80 for i in range(200)) + "\n```"
    parts = answer_view.split(code, rich=False)
    assert len(parts) > 1 and all(p.startswith("```python\n") and p.endswith("\n```") for p in parts)


def test_follow_ups_and_extra_rows_sit_on_the_last_part_only() -> None:
    text = "\n\n".join("word " * 500 for _ in range(3))
    views = answer_view.views(Answer(7, text, followups=["Go on"]), wrapped=False, rich=False)
    assert len(views) > 1 and all(not v.rows for v in views[:-1])
    last = views[-1]
    assert last.state["followups"] == ["Go on"] and last.rows[0][0].arg == {"text": "Go on", "turn_id": 7}
