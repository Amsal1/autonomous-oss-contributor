"""The watcher's reads (DESIGN §9.12; owner decision 2026-10-23 (b)): issue comments, pull reviews and
inline review comments; bots (``user.type == "Bot"`` or a ``[bot]`` login) and the owner are dropped in
code before the watch decision; the watermark is stamped before the reads."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from ossbot.runner.outputs import WatchDecision
from ossbot.runner.stages import watch

pytestmark = pytest.mark.unit
CID = "c_0123456789"
REPO = "o/r"
SINCE = "2026-10-23T10:00:00Z"
STAMP = "2026-10-23T11:00:00Z"
BOT = {"login": "coderabbitai[bot]", "type": "Bot"}
MAINTAINER = {"login": "maintainer", "type": "User"}


class Gh:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.pr: dict[str, Any] = {"state": "open"}
        self.issue_comments: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.inline: list[dict[str, Any]] = []
        self.calls: list[tuple[Any, ...]] = []

    def pull(self, repo: str, number: int) -> dict[str, Any]:
        self.log.append("read")
        return self.pr

    def comments(self, repo: str, number: int, since: str | None = None) -> list[dict[str, Any]]:
        self.log.append("read")
        self.calls.append(("comments", repo, number, since))
        return self.issue_comments

    def pull_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        self.log.append("read")
        self.calls.append(("pull_reviews", repo, number))
        return self.reviews

    def pull_review_comments(self, repo: str, number: int, since: str | None = None) -> list[dict[str, Any]]:
        self.log.append("read")
        self.calls.append(("pull_review_comments", repo, number, since))
        return self.inline


class Kiro:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.decision = WatchDecision(action="none")

    def invoke(self, cid: str, role: str, model: Any, text: str, **kw: Any) -> Any:
        self.texts.append(text)
        return SimpleNamespace(model_actual="m", value=self.decision)


class Sc:
    def __init__(self, notes: dict[str, Any] | None = None, watch_rounds_max: int = 3) -> None:
        self.log: list[str] = []
        self.gh = Gh(self.log)
        self._kiro = Kiro()
        self.notes: dict[str, Any] = dict(notes or {})
        self.said: list[str] = []
        self.transitions: list[tuple[str, str, dict[str, Any]]] = []
        self.clocks = SimpleNamespace(wall=lambda: 1000.0)
        self._config = SimpleNamespace(profile=SimpleNamespace(github_login="Octo-Owner"),
                                       policy=SimpleNamespace(limits=SimpleNamespace(
                                           watch_rounds_max=watch_rounds_max)))  # fmt: skip

    def config(self) -> Any:
        return self._config

    def kiro(self) -> Kiro:
        return self._kiro

    def load(self, cid: str) -> dict[str, Any]:
        return dict(self.notes)

    def save(self, cid: str, **values: Any) -> None:
        self.notes.update(values)

    def say(self, text: str) -> None:
        self.said.append(text)

    def transition(self, cid: str, from_: str, to: str, evidence: dict[str, Any]) -> None:
        self.transitions.append((from_, to, evidence))


@pytest.fixture(autouse=True)
def _no_stage_run(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.contextmanager
    def stage_run(*a: Any, **kw: Any) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(model_actual=None)

    monkeypatch.setattr(watch, "StageRun", stage_run)


def _run(sc: Sc, monkeypatch: pytest.MonkeyPatch, watch_rounds: int = 0) -> None:
    def stamp() -> str:
        sc.log.append("stamp")
        return STAMP

    monkeypatch.setattr(watch, "now_stamp", stamp)
    watch.run(sc, {"id": CID, "upstream": REPO, "pr_number": 9, "watch_rounds": watch_rounds})  # type: ignore[arg-type]


def _bot_items(sc: Sc) -> None:
    sc.gh.issue_comments = [{"user": BOT, "body": "## Walkthrough ...", "created_at": "2026-10-23T10:01:00Z"}]
    sc.gh.reviews = [{"id": 1, "user": BOT, "state": "COMMENTED", "body": "Actionable comments posted: 1",
                      "submitted_at": "2026-10-23T10:02:00Z"}]  # fmt: skip
    sc.gh.inline = [{"id": 7, "user": {"login": "coderabbitai[bot]"}, "path": "src/x.ts", "line": 120,
                     "body": "Ignore sessionStorage events.", "created_at": "2026-10-23T10:03:00Z"}]  # fmt: skip


def test_only_bot_items_never_reach_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    sc = Sc({"watermark": SINCE})
    _bot_items(sc)
    sc._kiro.decision = WatchDecision(action="fix", fix_request="x")  # would start a round if asked
    _run(sc, monkeypatch)
    assert sc._kiro.texts == [] and sc.transitions == [] and sc.said == []
    assert sc.notes["watermark"] == STAMP  # bot items still move the watermark


def test_maintainer_items_reach_the_task_with_their_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    sc = Sc({"watermark": SINCE})
    _bot_items(sc)
    sc.gh.reviews.append({"id": 2, "user": MAINTAINER, "state": "CHANGES_REQUESTED", "body": "",
                          "submitted_at": "2026-10-23T10:05:00Z"})  # fmt: skip
    sc.gh.inline.append({"id": 8, "user": MAINTAINER, "path": "src/y.ts", "original_line": 4,
                         "body": "Rename this.", "created_at": "2026-10-23T10:04:00Z"})  # fmt: skip
    _run(sc, monkeypatch)
    (text,) = sc._kiro.texts
    assert "## maintainer (inline on src/y.ts:4)\nRename this." in text
    assert "## maintainer (review: CHANGES_REQUESTED)" in text
    assert text.index("inline on src/y.ts:4") < text.index("review: CHANGES_REQUESTED")  # time order
    assert "coderabbitai" not in text and "Ignore sessionStorage" not in text and "Walkthrough" not in text
    assert '<untrusted source="github:o/r#9 new comments"' in text


def test_old_or_empty_reviews_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    sc = Sc({"watermark": SINCE})
    sc.gh.reviews = [
        {"id": 3, "user": MAINTAINER, "state": "COMMENTED", "body": "old", "submitted_at": "2026-10-23T09:00:00Z"},
        {"id": 4, "user": MAINTAINER, "state": "APPROVED", "body": "", "submitted_at": "2026-10-23T10:30:00Z"},
    ]
    _run(sc, monkeypatch)
    assert sc._kiro.texts == []
    assert sc.notes["watermark"] == SINCE  # nothing new: the old watermark stays


def test_the_owner_is_dropped_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    sc = Sc({"watermark": SINCE})
    sc.gh.issue_comments = [{"user": {"login": "octo-owner"}, "body": "my own note", "created_at": SINCE}]
    _run(sc, monkeypatch)
    assert sc._kiro.texts == []


def test_the_three_endpoints_with_since(monkeypatch: pytest.MonkeyPatch) -> None:
    sc = Sc({"watermark": SINCE})
    _run(sc, monkeypatch)
    assert sc.gh.calls == [("comments", REPO, 9, SINCE), ("pull_reviews", REPO, 9),
                           ("pull_review_comments", REPO, 9, SINCE)]  # fmt: skip


def test_the_watermark_is_stamped_before_the_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    sc = Sc({"watermark": SINCE})
    sc.gh.issue_comments = [{"user": MAINTAINER, "body": "please add a test", "created_at": "2026-10-23T10:10:00Z"}]
    _run(sc, monkeypatch)
    assert sc.log[0] == "stamp" and sc.log.count("read") == 4
    assert sc.notes["watermark"] == STAMP


def test_no_items_keep_the_old_watermark(monkeypatch: pytest.MonkeyPatch) -> None:
    sc = Sc({"watermark": SINCE})
    _run(sc, monkeypatch)
    assert sc.notes["watermark"] == SINCE and sc._kiro.texts == []


def test_rounds_cap_only_notifies_and_counts_humans(monkeypatch: pytest.MonkeyPatch) -> None:
    sc = Sc({"watermark": SINCE}, watch_rounds_max=3)
    _bot_items(sc)
    sc.gh.issue_comments.append({"user": MAINTAINER, "body": "ping", "created_at": "2026-10-23T10:20:00Z"})
    _run(sc, monkeypatch, watch_rounds=3)
    assert sc._kiro.texts == [] and sc.transitions == []
    assert sc.said == ["👀 PR #9 on o/r needs your attention (1 new comments)."]
