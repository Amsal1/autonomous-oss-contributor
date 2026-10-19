"""AC 1 and AC 53 over the production view builders (F009-R1-4): every real builder that takes untrusted
text renders within the §14.17.4 budgets in classic and rich mode with random and maximal input, its
keyboard passes the label rules, and maximal untrusted input never reaches ``ViewTooLarge``.

Builders: S1 per kind (``open_pr`` r1 and rN with "You asked", ``push_update``, ``comment``,
``issue_claim``), S12 stale renders, the explorer (9 sections), file and interdiff views, the draft
review, the dashboard, the proposal card and Details (6 sections), the rescope and ceiling cards,
``Working on it``, link and refusal cards, the edit-text prompts, ``/size`` and ``/filters``, the chat answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ossbot.common.ids import new_token
from ossbot.common.schemas import ProposalSubmit, State
from ossbot.gatekeeper import db
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.proposals import ProposalService
from ossbot.gatekeeper.telegram import cards, chat_relay, proposal_cards, rescope_cards, size_pickers, work_links
from ossbot.gatekeeper.telegram.cards import AskedItem, CardView, NumstatEntry
from ossbot.gatekeeper.telegram.dashboard import PURPOSES, Dashboard
from ossbot.gatekeeper.telegram.review import comments, edit_text, explorer, patchview, stale
from ossbot.gatekeeper.telegram.ui import blocks, classic, rich
from ossbot.gatekeeper.telegram.ui import kit as k
from ossbot.gatekeeper.telegram.ui import send as ui_send
from ossbot.gatekeeper.telegram.ui.limits import CLASSIC_MAX, check_keyboard
from ossbot.gatekeeper.telegram.ui.sanitize import parse_github_url
from tests.conftest import insert_action, insert_contribution, packaged_contribution
from tests.unit.gatekeeper.test_proposals import sample

untrusted = st.text(st.characters(codec="utf-8"), max_size=6000)
HAZARD = '<b>&</b> /dryrun off tg://x <tg-button type="callback_data" data="x"> ** \u202e\u200b'
MAXIMAL = (HAZARD * 400)[:6000]
EXPIRES = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
Out = list[k.View | str]


def one_line(text: str, n: int) -> str:
    return " ".join(text.split())[:n] or "x"


def patch_with(text: str) -> str:
    body = "".join(f"+{one_line(line, 300)}\n" for line in (text.splitlines() or ["x"])[:200])
    count = body.count("\n")
    return (f"diff --git a/src/{one_line(text, 40).replace(' ', '_').replace('/', '_')}.py b/src/x.py\n"
            f"--- a/src/x.py\n+++ b/src/x.py\n@@ -0,0 +1,{count} @@\n{body}"
            "diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
            "@@ -0,0 +1,1 @@\n+def test_x(): pass\n")  # fmt: skip


# ------------------------------------------------------------------------------ S1 and S12 (no DB)
def card_view(text: str, kind: str = "open_pr", **over: Any) -> CardView:
    base: dict[str, Any] = {
        "kind": kind, "cid": "c_0123456789", "upstream": "langfuse/langfuse", "dry_run": True,
        "title": one_line(text, 256), "body": text or "b", "commit_message": text or "m",
        "numstat": tuple(NumstatEntry(3, 1, f"src/{one_line(text, 900)}{i}.py") for i in range(40)),
        "head_sha": "3" * 40, "round_base": "1" * 40, "base_ref": "main", "identity": "Amsal Khan <a@b.c>",
        "branch": "ossbot/1-x", "fork_owner": "test-bot-login", "pr_number": 7 if kind != "open_pr" else None,
        "target_number": 7, "rev": 1, "issue_number": 1234, "issue_title": text[:300],
        "issue_url": "https://github.com/langfuse/langfuse/issues/1234", "user_directed": True, "notes": text[:1000],
        "size_report": {"approved_class": "s", "measured_class": "xl", "estimate": {"code_lines": 40},
                        "measured": {"files": 40, "code_added": 900, "code_deleted": 90, "test_added": 300},
                        "outside_areas": [text[:200]] * 20},
        "validation": {"commands": [{"name": text[:60], "exit_code": 1, "tail": text}] * 12},
        "review": {"verdict": text[:200], "blocking": [text[:200]] * 10, "nits": [text[:200]] * 10,
                   "size_reason": text[:500]},
        "models": {"coder": "a", "reviewer": "a"}, "risk": text[:200], "confidence": 0.5, "rationale": text,
        "expires_at": EXPIRES,
    }  # fmt: skip
    base.update(over)
    return CardView(**base)


def s1_cards(text: str) -> Out:
    asked = tuple(AskedItem(f"src/x.py L{i}", text[:4000], touched=None, coder=text[:200]) for i in range(30))
    views = [card_view(text), card_view(text, rev=3, asked=asked, asked_total=30,
                                        interdiff={"mode": "interdiff", "files": [{"path": text[:200]}] * 50}),
             card_view(text, "push_update"), card_view(text, "comment", body=text[:20000] or "c"),
             card_view(text, "issue_claim", body=text[:20000] or "c")]  # fmt: skip
    out: Out = []
    for v in views:
        msgs = cards.render(v)
        assert not msgs.too_large, v.kind  # AC 53: maximal untrusted input never reaches ViewTooLarge
        out += [msgs.card, *msgs.parts]
    return out


def stale_cards(ctx: AppContext, text: str) -> Out:
    """S12 of each S1 kind, for an invalidation, an expiry and a superseded proposal."""
    out: Out = []
    for card in [c for c in s1_cards(text) if isinstance(c, str) and "PR body (" not in c][:5]:
        for status, reason in (("invalidated", "binding_changed"), ("expired", None),
                               ("invalidated", "new_proposal_version")):  # fmt: skip
            out.append(stale.render(ctx.conn, card_html=card, status=status, reason=reason, cid="c_0123456789",
                                    rev=2, action_id=None).text)  # fmt: skip
    return out


def explorer_views(text: str) -> Out:
    contrib = {
        "issue_number": 1234, "issue_title": text[:300], "size_report_json": json.dumps({"measured_class": "m"}),
        "vetting_json": json.dumps({"summary": text[:2000]}), "plan_json": json.dumps({"summary": text[:2000]}),
        "risk": text[:100], "confidence": 0.4,
        "validation_json": json.dumps({"commands": [{"name": text[:60], "exit_code": 1, "tail": text}] * 12}),
        "review_json": json.dumps({"verdict": text[:200], "blocking": [{"issue": text[:500]}] * 10,
                                   "nits": [text[:500]] * 10}),
        "models_json": json.dumps({"coder": "a", "reviewer": "b"}), "pr_title": one_line(text, 256),
        "pr_body": text, "pr_title_override": None, "pr_body_override": None,
    }  # fmt: skip
    files = patchview.parse(patch_with(text))
    s = explorer.Subject("c_0123456789", 2, "x_0123456789", "open_pr", "langfuse/langfuse", contrib, files)  # type: ignore[arg-type]
    out: Out = [explorer.explorer_view(s, section) for section in range(1, 10)]
    out += [explorer.file_view(s, 0, page) for page in (1, 2, 3)]
    out += [explorer.file_view(s, 0, picker_page=1), explorer.file_view(s, 0, hunk_picker=1)]
    inter = explorer.file_view(replace(s, source="interdiff"), 1)
    inter.kind = "interdiff"
    return [*out, inter]


def chat_answer(text: str) -> Out:
    part = chat_relay.chunks(text)[0]
    return [k.View(kind="chat_answer", rich=True, blocks=chat_relay.quoted_answer(chat_relay.HEADER, part))]


def link_views(text: str) -> Out:
    ref = parse_github_url("https://github.com/langfuse/langfuse/issues/1234")
    assert ref is not None
    info = {"title": text[:200], "state": text[:20]}
    return [
        work_links.link_view(ref, info, text[:1000]),
        work_links.link_view(ref, None, ""),
        work_links.refusal_view("langfuse/langfuse", {"policy_line": text[:300], "policy_path": "AI_POLICY.md"}),
    ]


PURE: dict[str, Callable[[str], Out]] = {"s1_and_s12": s1_cards, "explorer_file_interdiff": explorer_views,
                                         "chat_answer": chat_answer, "link_cards": link_views}  # fmt: skip


# ------------------------------------------------------------------------------ DB-backed builders
@contextmanager
def captured() -> Iterator[list[k.View]]:
    """The views a builder would send (``ui_send.deliver``), without enqueueing them."""
    seen: list[k.View] = []
    original = ui_send.deliver
    ui_send.deliver = lambda ctx, view, **kw: seen.append(view)  # type: ignore[assignment,misc]
    try:
        yield seen
    finally:
        ui_send.deliver = original  # type: ignore[assignment]


def proposal_views(ctx: AppContext, text: str) -> Out:
    cid = insert_contribution(ctx, State.SCOUTING, issue_title=text[:300], source="user", notes=text[:1000],
                              vetting_json=json.dumps({"claim_status": "unclaimed", "cla_required": True,
                                                       "requires_assignment": True}))  # fmt: skip
    doc = sample(problem=text[:2000] or "p", approach=[one_line(text, 300)] * 10,
                 areas=[one_line(text, 200)] * 100, tests=[one_line(text, 300)] * 50,
                 validation_plan=text[:1000], risks=[one_line(text, 200)] * 10, rationale=text[:500],
                 alternatives=[{"summary": one_line(text, 300)}] * 10, overview=text[:8000])  # fmt: skip
    out = ProposalService(ctx).submit(cid, ProposalSubmit.model_validate({"proposal": doc}), "uid:2001/worker")
    row = db.one(ctx.conn, "SELECT * FROM proposal_approvals WHERE id = ?", (out["approval_id"],))
    view = proposal_cards.card_view(ctx, row)
    assert view is not None
    rendered = proposal_cards.render(ctx, row)
    assert not rendered.too_large
    return [view, rendered.card, *(proposal_cards.details_view(ctx, cid, 1, s) for s in range(1, 7))]


def size_views(ctx: AppContext, text: str) -> Out:
    report = {"approved_class": "s", "measured_class": "xxl", "estimate": {"code_lines": 35},
              "measured": {"files": 91, "code_added": 3180, "code_deleted": 240, "test_added": 1020},
              "outside_areas": [text[:200]] * 30}  # fmt: skip
    cid = insert_contribution(ctx, State.RESCOPE_REQUIRED, proposal_version=1,
                              review_json=json.dumps({"size_reason": text[:2000]}))  # fmt: skip
    ref = parse_github_url("https://github.com/langfuse/langfuse/issues/9")
    assert ref is not None
    return [rescope_cards.view(ctx, cid, 1, "overrun", report), rescope_cards.view(ctx, cid, 1, "ceiling", report),
            work_links.working_view(ctx, cid, ref, {"title": text[:200], "state": "open"}),
            size_pickers.size_view(ctx), size_pickers.size_view(ctx, "repos"),
            *(size_pickers.filters_view(ctx, p) for p in range(1, 5))]  # fmt: skip


def review_views(ctx: AppContext, text: str) -> Out:
    cid, action = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE contributions SET pr_title = ?, pr_body = ? WHERE id = ?",
                         (one_line(text, 256), text or "b", cid))  # fmt: skip
        for i in range(30):
            ctx.conn.execute("INSERT INTO review_comments (id, cid, rev, source, path, side, line_start, line_end,"
                             " quote, text, created_at) VALUES (?,?,1,'telegram',?,?,?,?,?,?,?)",
                             (new_token(), cid, f"src/{one_line(text, 300)}", "new", i + 1, i + 1, text[:4000],
                              text[:4000] or "c", f"2026-10-19T00:00:{i:02d}Z"))  # fmt: skip
    comment = insert_action(ctx, cid, "comment", {"target_number": 7, "body": text[:20000] or "c"})
    out: Out = [comments.draft_view(ctx, cid, 1, action), comments.draft_view(ctx, cid, 1, action, show_all=True)]
    with captured() as seen:
        edit_text.open_picker(ctx, cid, 1, action, "open_pr", card_message=1)
        edit_text.prompt_comment(ctx, cid, comment, reply_to=1)
        comments.line_picker(ctx, cid, 1, action, "src/x.py", [("new", i, i) for i in range(1, 7)], text[:4000] or "c",
                             reply_to=1)  # fmt: skip
    return out + seen


def dashboard_view(ctx: AppContext, text: str) -> Out:
    if ctx.control.view().state == "running":
        ctx.control.pause(one_line(text, 500), "tg:4242")
    tokens = dict.fromkeys(PURPOSES, "A" * 22)
    return [Dashboard(ctx).render(tokens), Dashboard(ctx).render(tokens, kill_armed="B" * 22)]


WITH_DB: dict[str, Callable[[AppContext, str], Out]] = {
    "stale": stale_cards, "proposal": proposal_views, "size_cards": size_views, "review": review_views,
    "dashboard": dashboard_view,
}  # fmt: skip


# ------------------------------------------------------------------------------ the checks
def check(item: k.View | str) -> None:
    if isinstance(item, str):  # an already rendered classic message (S1 card, part, S12)
        assert len(item) <= CLASSIC_MAX
        assert item.count("<blockquote") == item.count("</blockquote>")
        return
    item.check()  # rows, buttons per row and label lengths (§14.17.2)
    check_keyboard(item.keyboard_labels(), max_rows=item.max_rows)
    out = classic.render(item)  # raises ViewTooLarge: AC 53 says it never does on untrusted input
    assert len(out.text) <= CLASSIC_MAX, item.kind
    assert out.text.count("<blockquote") == out.text.count("</blockquote>")
    message = rich.render(replace(item, rich=True))  # the rich form of every view, whether it is used or not
    blocks.check_budget(message)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@pytest.mark.parametrize("name", list(PURE))
@given(text=untrusted)
def test_pure_builders_fit_their_budgets(name: str, text: str) -> None:
    for item in PURE[name](text):
        check(item)


@settings(max_examples=8, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])  # fmt: skip
@pytest.mark.parametrize("name", list(WITH_DB))
@given(text=untrusted)
def test_db_builders_fit_their_budgets(ctx: AppContext, name: str, text: str) -> None:
    for item in WITH_DB[name](ctx, text):
        check(item)


@pytest.mark.parametrize("name", [*PURE, *WITH_DB])
def test_maximal_untrusted_input_never_reaches_view_too_large(ctx: AppContext, name: str) -> None:
    items = PURE[name](MAXIMAL) if name in PURE else WITH_DB[name](ctx, MAXIMAL)
    assert items
    for item in items:
        check(item)
