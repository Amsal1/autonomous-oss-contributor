"""The U14 choke point (DESIGN §14.9, AC 66, BA-16): display text is redacted, a stream never shows
a secret's prefix through ``draft_visible``, verbatim publish bytes are scanned and never altered,
and packaging refuses the gatekeeper's own secret values (``gatekeeper_secret``)."""

from __future__ import annotations

import logging

import pytest

from ossbot.common.hashing import sha256_text
from ossbot.common.redact import REDACTED
from ossbot.gatekeeper import policy
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.ui import redact as ui
from tests.fakes.fake_hermes import FakeHermesStream

HERMES_KEY = "hk_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
GHP = "ghp_" + "Q8xk2LmZ9vR4tYp7" + "Wn3bJc6Hd1Fs5Ga0Ke8U"
KIRO = "ksk_" + "kirokeyvalue0123456789abcd"


@pytest.fixture
def scanner_ctx(ctx: AppContext) -> AppContext:
    ctx.env = {"HERMES_API_KEY": HERMES_KEY, "KIRO_KEY_SHA256": sha256_text(KIRO)}
    ctx.reset_secret_scanner()
    return ctx


def _thirds(secret: str) -> list[str]:
    n = len(secret)
    return [secret[: n // 3], secret[n // 3 : 2 * n // 3], secret[2 * n // 3 :]]


def _no_prefix(text: str, secret: str, min_len: int = 8) -> bool:
    return all(secret[:k] not in text for k in range(min_len, len(secret) + 1))


@pytest.mark.parametrize("secret", [HERMES_KEY, GHP, KIRO])
async def test_split_secret_never_shows_a_prefix_in_any_draft(scanner_ctx: AppContext, secret: str) -> None:
    stream = FakeHermesStream(["The value is ", *_thirds(secret), " and that is all."], hold_s=0.0)
    text = ""
    async for delta in stream.deltas():
        text += delta
        assert _no_prefix(ui.draft_visible(text), secret)  # every draft along the way
    final, hits = ui.redact(text)
    assert hits >= 1 and secret not in final and REDACTED in final


async def test_stop_after_the_second_of_three_deltas(scanner_ctx: AppContext) -> None:
    parts = ["Here: ", *_thirds(HERMES_KEY)]
    text = "".join(parts[:3])  # Stop tapped after the 2nd third arrived
    partial = ui.draft_visible(text) + "…"
    assert partial == "Here:…"  # ends before the key's first character, then …


def test_truncated_inside_a_token(scanner_ctx: AppContext) -> None:
    body = "x " * 1000 + GHP[:20]  # the 200 KB cut lands inside a ghp_ token
    cut = ui.draft_visible(body) + "…(truncated)"
    assert _no_prefix(cut, GHP, 4)


def test_verbatim_bytes_are_scanned_never_altered(scanner_ctx: AppContext, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    title = f"Fix {HERMES_KEY}"
    assert ui.scan(title, surface="pr_title") == 1
    assert title == f"Fix {HERMES_KEY}"  # unchanged
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "redaction_verbatim_hit"]
    assert HERMES_KEY not in record.getMessage()


def test_display_redaction_logs_counts_not_values(scanner_ctx: AppContext, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    out, hits = ui.redact(f"log tail {GHP}", surface="log_tail")
    assert hits == 1 and GHP not in out
    assert all(GHP not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("kind", ["title", "body", "commit_message", "comment"])
def test_packaging_text_refuses_gatekeeper_secret_values(scanner_ctx: AppContext, kind: str) -> None:
    text = f"value {HERMES_KEY}" + ("\nAI assistance disclosure\n<!-- ossbot:cid=x -->" if kind == "body" else "")
    found = policy.check_text(kind, text, scanner_ctx.config, set(),  # type: ignore[arg-type]
                              secret_values=scanner_ctx.secret_scanner.values)  # fmt: skip
    hits = [v for v in found if v.code == "gatekeeper_secret"]
    assert hits and hits[0].detail == kind and HERMES_KEY not in hits[0].detail
