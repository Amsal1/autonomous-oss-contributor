"""S6 interdiff (DESIGN §14.11.7, ADR-0019; AC 9): same base → a real patch with line-window touch flags;
moved base → range-diff with ``files[]`` from the normalized per-file patch comparison and file-level
touch flags; a missing previous mirror → unavailable; the revised card and the 🔀 view."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.gitcmd import INTERDIFF, RANGE_DIFF
from ossbot.gatekeeper import db, interdiff
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import cards
from tests.conftest import insert_action, packaged_contribution
from tests.telegram.reviewkit import NUMSTAT, PATCH, comments, labels, publish, text, token, view_message, write_patch
from tests.telegram.tgharness import TgRig, make_rig

PATCH_R2 = PATCH.replace("+y = 3", "+y = 4")
INTERDIFF_PATCH = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -3,1 +3,1 @@
-y = 3
+y = 4
"""
RANGE = """1:  aaaaaaa ! 1:  bbbbbbb Fix the thing
    @@ src/app.py
    ## src/app.py ##
    -+y = 3
    ++y = 4
    ## tests/test_app.py ##
     +def test_x():
"""


@dataclass
class Run:
    stdout: bytes


@dataclass
class FakeGit:
    out: bytes
    calls: list[tuple[list[str], Path]] = field(default_factory=list)
    alternates_seen: list[str] = field(default_factory=list)

    def run(self, args: list[str], cwd: Path) -> Run:
        self.calls.append((args, cwd))
        alt = cwd / "objects" / "info" / "alternates"
        self.alternates_seen.append(alt.read_text(encoding="utf-8") if alt.exists() else "")
        return Run(self.out)


@dataclass
class FakeOps:
    root: Path
    git: FakeGit

    def derived_dir(self, cid: str, rev: int) -> Path:
        return self.root / "derived" / cid / str(rev)

    def mirror_dir(self, upstream: str, cid: str, rev: int) -> Path:
        return self.root / "mirrors" / f"{cid}-r{rev}.git"


def _ops(tmp_path: Path, out: str, *, old_mirror: bool = True) -> FakeOps:
    ops = FakeOps(tmp_path, FakeGit(out.encode()))
    ops.mirror_dir("u/r", "c_X", 2).mkdir(parents=True)
    if old_mirror:
        ops.mirror_dir("u/r", "c_X", 1).mkdir(parents=True)
    for rev, patch in ((1, PATCH), (2, PATCH_R2)):
        ops.derived_dir("c_X", rev).mkdir(parents=True)
        (ops.derived_dir("c_X", rev) / "change.patch").write_text(patch, encoding="utf-8")
    return ops


def _compute(ops: Any, old_base: str, new_base: str) -> dict[str, Any]:
    return interdiff.compute(ops, upstream="u/r", cid="c_X", rev=2, old={"round_base": old_base, "head": "h1"},
                             new={"round_base": new_base, "head": "h2"})  # fmt: skip


def test_same_base_is_a_real_interdiff(tmp_path: Path) -> None:
    ops = _ops(tmp_path, INTERDIFF_PATCH)
    info = _compute(ops, "b1", "b1")
    assert info == {"mode": "interdiff", "base_changed": False, "touch_granularity": "line",
                    "files": [{"path": "src/app.py", "added": 1, "deleted": 1, "new_ranges": [[3, 3]]}]}  # fmt: skip
    ((args, cwd),) = ops.git.calls
    assert args == [*INTERDIFF, "h1", "h2"] and cwd == ops.mirror_dir("u/r", "c_X", 2)
    assert str((ops.mirror_dir("u/r", "c_X", 1) / "objects").resolve()) in ops.git.alternates_seen[0]
    assert not (ops.mirror_dir("u/r", "c_X", 2) / "objects" / "info" / "alternates").exists()  # removed after
    saved = json.loads((ops.derived_dir("c_X", 2) / "interdiff.json").read_text(encoding="utf-8"))
    assert saved == info
    assert (ops.derived_dir("c_X", 2) / "interdiff.patch").read_text(encoding="utf-8") == INTERDIFF_PATCH


def test_moved_base_uses_range_diff_and_normalized_files(tmp_path: Path) -> None:
    ops = _ops(tmp_path, RANGE)
    info = _compute(ops, "b1", "b2")
    assert info["mode"] == "range_diff" and info["touch_granularity"] == "file"
    assert info["files"] == [{"path": "src/app.py", "added": 1, "deleted": 1}]
    assert ops.git.calls[0][0] == [*RANGE_DIFF, "b1..h1", "b2..h2"]


def test_range_files_ignore_moved_hunk_headers() -> None:
    moved = PATCH.replace("@@ -1,4 +1,5 @@", "@@ -10,4 +10,5 @@")
    assert interdiff.range_files(PATCH, moved) == []


def test_missing_previous_mirror_is_unavailable(tmp_path: Path) -> None:
    ops = _ops(tmp_path, "", old_mirror=False)
    info = _compute(ops, "b1", "b1")
    assert info == {"mode": "unavailable", "reason": "previous revision gone"}
    assert ops.git.calls == []


@pytest.mark.parametrize(
    ("comment", "info", "expected"),
    [
        (
            {"path": "src/app.py", "line_start": 6, "line_end": 6},
            {"mode": "interdiff", "files": [{"path": "src/app.py", "new_ranges": [[3, 3]]}]},
            True,
        ),  # within ±3
        (
            {"path": "src/app.py", "line_start": 7, "line_end": 7},
            {"mode": "interdiff", "files": [{"path": "src/app.py", "new_ranges": [[3, 3]]}]},
            False,
        ),
        ({"path": "other.py", "line_start": 3}, {"mode": "interdiff", "files": [{"path": "src/app.py"}]}, False),
        ({"path": "src/app.py", "line_start": 90}, {"mode": "range_diff", "files": [{"path": "src/app.py"}]}, True),
        ({}, {"mode": "range_diff", "files": [{"path": "src/app.py"}]}, True),
        ({"path": "src/app.py"}, {"mode": "unavailable"}, None),
    ],
)
def test_touched(comment: dict[str, Any], info: dict[str, Any], expected: bool | None) -> None:
    assert cards.touched(comment, info) is expected


@pytest.fixture
async def rig(ctx: AppContext) -> AsyncIterator[TgRig]:
    async for r in make_rig(ctx):
        yield r


def _revised(ctx: AppContext, info: dict[str, Any], patch_text: str) -> tuple[str, str]:
    """An open_pr at rev 2 answering a consumed change request, with its interdiff files."""
    cid, first = packaged_contribution(ctx)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE outbound_actions SET status = 'cancelled' WHERE id = ?", (first,))
        ctx.conn.execute("UPDATE contributions SET diffstat_json = ? WHERE id = ?", (NUMSTAT, cid))
        ctx.conn.execute("INSERT INTO change_requests (id, cid, text, created_at, consumed_at, comments_json)"
                         " VALUES ('r_1', ?, 'x', '2026-10-17T00:00:00Z', '2026-10-17T01:00:00Z', ?)",
                         (cid, json.dumps([{"path": "src/app.py", "line_start": 3, "line_end": 3,
                                            "text": "y should be 4"}])))  # fmt: skip
    action = insert_action(ctx, cid, "open_pr", {"rev": 2, "head_sha": "5" * 40, "branch": "ossbot/101-fix-abc123",
                                                 "fork_owner": "test-bot-login"})  # fmt: skip
    write_patch(ctx, cid, 2, PATCH_R2)
    out = ctx.gitops.derived_dir(cid, 2)
    (out / "interdiff.json").write_text(json.dumps(info), encoding="utf-8")
    (out / "interdiff.patch").write_text(patch_text, encoding="utf-8")
    return cid, action


async def test_revised_card_and_the_interdiff_view(rig: TgRig) -> None:
    ctx = rig.ctx
    info = {"mode": "interdiff", "files": [{"path": "src/app.py", "added": 1, "deleted": 1, "new_ranges": [[3, 3]]}]}
    cid, action = _revised(ctx, info, INTERDIFF_PATCH)
    card = await publish(rig, action)
    shown = text(rig, card.message_id)
    assert shown.startswith(f"🔁 <b>Revised for review</b> · {cid} · r2")
    assert "1. src/app.py L3 — y should be 4 · ✅ touched" in shown
    assert "<b>What changed since r1</b> ✅ (base unchanged): src/app.py +1 −1" in shown
    assert labels(rig, card.message_id)[2:4] == ["🧭 Explore", "🔀 Interdiff"]
    await rig.tap(token(rig, card.message_id, "🔀 Interdiff"), message=card.message_id)
    view = view_message(ctx, "interdiff")
    assert f"🔀 <b>{cid} r1 → r2</b>" in text(rig, view) and "+y = 4" in text(rig, view)


LONG = "".join(f"+interdiff line {i:04d} padding padding\n" for i in range(160))
TWO_FILES = (f"diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -3,1 +3,161 @@\n"
             f"-y = 3\n+y = 4\n{LONG}"
             "diff --git a/tests/test_app.py b/tests/test_app.py\n--- a/tests/test_app.py\n+++ b/tests/test_app.py\n"
             "@@ -1,2 +1,3 @@\n def test_x():\n     assert True\n+    assert only_in_the_interdiff()\n")  # fmt: skip


async def test_a_multi_file_multi_page_interdiff_stays_inside_the_interdiff(rig: TgRig) -> None:
    """F009-R1-3: file navigation, paging, the hunk picker and quote replies use interdiff.patch."""
    ctx = rig.ctx
    info = {"mode": "interdiff", "files": [{"path": "src/app.py", "added": 161, "deleted": 1, "new_ranges": [[3, 163]]},
                                           {"path": "tests/test_app.py", "added": 1, "deleted": 0,
                                            "new_ranges": [[3, 3]]}]}  # fmt: skip
    cid, action = _revised(ctx, info, TWO_FILES)
    card = await publish(rig, action)
    await rig.tap(token(rig, card.message_id, "🔀 Interdiff"), message=card.message_id)
    view = view_message(ctx, "interdiff")
    first = text(rig, view)
    assert "file 1/2" in first and "page 1/2" in first and "interdiff line 0000" in first
    await rig.tap(token(rig, view, "▶"), message=view)  # the page pager
    assert view_message(ctx, "interdiff") == view  # edited in place, still the interdiff view
    second = text(rig, view)
    assert "page 2/2" in second and "interdiff line 0159" in second and "interdiff line 0000" not in second
    await rig.tap(token(rig, view, "Next file ▶"), message=view)
    shown = text(rig, view)
    assert "file 2/2" in shown and "only_in_the_interdiff" in shown
    assert db.one(ctx.conn, "SELECT kind FROM tg_views WHERE message_id = ?", (view,))["kind"] == "interdiff"  # type: ignore[index]
    # the hunk picker offers the interdiff's hunks and stores the interdiff hunk's range
    await rig.tap(token(rig, view, "💬 Comment"), message=view)
    assert labels(rig, view)[:1] == ["H1"]
    await rig.tap(token(rig, view, "H1"), message=view)
    prompt = rig.message_id_of(-1)
    await rig.say("Why a second assert?", reply_to=prompt)
    (row,) = comments(ctx, cid)
    assert (row["path"], row["line_start"], row["line_end"], row["rev"]) == ("tests/test_app.py", 3, 3, 2)
    # a quote reply on the interdiff view is located like on a file view
    await rig.tap(token(rig, view, "↩️ Back"), message=view)
    await rig.say("Name it better", reply_to=view, quote="assert only_in_the_interdiff()")
    rows = comments(ctx, cid)
    assert len(rows) == 2 and (rows[1]["path"], rows[1]["line_start"], rows[1]["side"]) == ("tests/test_app.py", 3,
                                                                                            "new")  # fmt: skip


async def test_a_quote_on_the_range_diff_is_a_file_level_comment(rig: TgRig) -> None:
    ctx = rig.ctx
    info = {"mode": "range_diff", "old_base": "1" * 40, "new_base": "2" * 40,
            "files": [{"path": "src/app.py", "added": 1, "deleted": 1}]}  # fmt: skip
    cid, action = _revised(ctx, info, RANGE)
    card = await publish(rig, action)
    assert "line-level touch check unavailable" in text(rig, card.message_id)
    await rig.tap(token(rig, card.message_id, "🔀 Interdiff"), message=card.message_id)
    view = view_message(ctx, "interdiff")
    assert "base moved: line-level touch check unavailable" in text(rig, view)
    await rig.say("Why 4?", reply_to=view, quote="++y = 4")
    (row,) = comments(ctx, cid)
    assert (row["path"], row["line_start"], row["rev"]) == ("src/app.py", None, 2)
