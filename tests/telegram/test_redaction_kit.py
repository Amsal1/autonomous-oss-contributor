"""U14 in the kit (DESIGN §14.3; AC 66 and BA-16, FEAT-009 part): every untrusted leaf of a classic or rich
message passes ``telegram/ui/redact.py`` before it is sent, while verbatim publish bytes are only scanned.
The streaming paths (drafts, ``chat/stream.py``) join this test with FEAT-010 item 49."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from ossbot.common.hashing import sha256_text
from ossbot.common.redact import REDACTED
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import chat_relay, work_links
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui.sanitize import parse_github_url
from tests.unit.gatekeeper.test_redaction_paths import GHP, HERMES_KEY, KIRO, _no_prefix

UI = Path(__file__).resolve().parents[2] / "src" / "ossbot" / "gatekeeper" / "telegram" / "ui"


@pytest.fixture
def scanner_ctx(ctx: AppContext) -> AppContext:
    ctx.env = {"HERMES_API_KEY": HERMES_KEY, "KIRO_KEY_SHA256": sha256_text(KIRO)}
    ctx.reset_secret_scanner()
    return ctx


def views(secret: str) -> list[k.View]:
    text = f"Here is the value: {secret} — keep it safe"
    ref = parse_github_url("https://github.com/langfuse/langfuse/issues/1")
    assert ref is not None
    part = chat_relay.chunks(text)[0]
    return [
        k.View(kind="chat_answer", rich=True, blocks=chat_relay.quoted_answer(chat_relay.HEADER, part)),
        work_links.link_view(ref, {"title": text, "state": "open"}, text),
        k.View(kind="fyi", blocks=[k.Block((k.claim("Planner", text), k.Quote(((k.Text(text, 1500),),)),
                                             k.PreBlock(text)))]),
    ]  # fmt: skip


@pytest.mark.parametrize("secret", [HERMES_KEY, GHP, KIRO])
def test_final_classic_and_rich_messages_are_redacted(scanner_ctx: AppContext, secret: str) -> None:
    for view in views(secret):
        out = classic.render(view)
        assert _no_prefix(out.text, secret) and REDACTED in out.text, view.kind
        doc = json.dumps(blocks.dump(rich.render(view)), ensure_ascii=False)
        assert _no_prefix(doc, secret) and REDACTED in doc, view.kind


def test_verbatim_publish_bytes_are_scanned_not_altered(scanner_ctx: AppContext) -> None:
    view = k.View(kind="fyi", blocks=[k.Block((k.Lines(((k.Verbatim(f"title {GHP}", "pr_title"),),)),))])
    out = classic.render(view)
    assert out.verbatim_hits == 1 and GHP in out.text  # a hit withholds the card upstream; bytes unchanged


def _calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)}  # fmt: skip


@pytest.mark.parametrize("module", ["classic.py", "rich.py"])
def test_both_renderers_go_through_the_one_redactor(module: str) -> None:
    """BA-16 (renderer part): the classic and rich renderers call ``ui_redact.redact`` for untrusted leaves
    and ``ui_redact.scan`` for verbatim bytes; neither calls ``common.redact`` directly."""
    calls = _calls(UI / module)
    assert {"ui_redact.redact", "ui_redact.scan"} <= calls
    source = (UI / module).read_text(encoding="utf-8")
    assert "from ossbot.common.redact" not in source and "Redactor(" not in source
