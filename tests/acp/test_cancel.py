"""Stop cancels the Kiro turn (owner check O5, ADR-0002 addendum post-v1): Hermes v0.22.2 calls
``client.cancel()`` from another thread when the client class defines it; the adapter sends ACP
``session/cancel`` and drains until the ``session/prompt`` response, with a bounded wait."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.acp.conftest import FakeRun, ask, client, v3, v3_core

REJECT = [
    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
]


def _start_turn(c: v3.KiroV3Client) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def turn() -> None:
        started = time.monotonic()
        try:
            box["out"] = ask(c)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertions
            box["err"] = exc
        box["took"] = time.monotonic() - started

    t = threading.Thread(target=turn, name="hermes-worker")
    t.start()
    return t, box


def _wait_prompt_sent(run: FakeRun, n: int = 1) -> None:
    for _ in range(200):
        if len(run.requests("session/prompt")) >= n:
            time.sleep(0.2)  # let the fake stream its first chunk
            return
        time.sleep(0.05)
    raise AssertionError("session/prompt never reached the fake")


def _cancel_from_a_stranger_thread(c: v3.KiroV3Client) -> float:
    started = time.monotonic()
    t = threading.Thread(target=c.cancel, name="hermes-interrupt")
    t.start()
    t.join(timeout=5)
    return time.monotonic() - started


def test_hermes_probe_sees_a_cancel_method() -> None:
    # the exact check in Hermes agent/client_lifecycle.py _abort_request_slot_client
    assert callable(getattr(v3.KiroV3Client, "cancel", None))


def test_cancel_sends_session_cancel_and_returns_the_partial_text(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(prompts=[[{"text": "partial"}, {"wait_cancel": True}], [{"text": "next"}]])
    c = client(run, tmp_path)
    t, box = _start_turn(c)
    _wait_prompt_sent(run)
    assert _cancel_from_a_stranger_thread(c) < 1.0  # non-blocking
    t.join(timeout=5)
    assert not t.is_alive()
    assert "err" not in box, box.get("err")
    assert box["out"].choices[0].message.content == "partial"
    assert box["took"] < 10
    cancels = run.requests("session/cancel")
    assert len(cancels) == 1
    assert cancels[0]["params"] == {"sessionId": run.requests("session/prompt")[0]["params"]["sessionId"]}
    assert "id" not in cancels[0]  # a notification
    assert c.last_turn.stop_reason == "cancelled"
    # the process survives: the next turn runs on it without a respawn
    assert ask(c).choices[0].message.content == "next"
    assert len(run.procs) == 1
    c.close()


def test_a_silent_agent_ends_the_turn_at_the_bound_and_the_next_turn_respawns(
    fake: Callable[..., FakeRun], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(v3, "CANCEL_GRACE_S", 0.5)
    run = fake(prompts=[[{"text": "partial"}, {"sleep": 30}], [{"text": "fresh"}]])
    c = client(run, tmp_path)
    t, box = _start_turn(c)
    _wait_prompt_sent(run)
    c.cancel()
    t.join(timeout=10)
    assert not t.is_alive()
    err = box.get("err")
    assert isinstance(err, v3_core.AcpError) and not isinstance(err, v3_core.TransportClosed)
    assert "Kiro did not confirm within 0.5 s" in str(err)
    assert box["took"] < 5
    assert ask(c).choices[0].message.content == "fresh"
    assert len(run.procs) == 2  # respawned
    c.close()


def test_a_permission_request_after_the_cancel_is_answered_cancelled(
    fake: Callable[..., FakeRun], tmp_path: Path
) -> None:
    run = fake(prompts=[[{"text": "partial"}, {"sleep": 1.0}, {"permission": REJECT}]])
    c = client(run, tmp_path)
    t, box = _start_turn(c)
    _wait_prompt_sent(run)
    c.cancel()
    t.join(timeout=10)
    assert "err" not in box, box.get("err")
    answers = [f["result"] for f in run.responses() if isinstance(f.get("result"), dict) and "outcome" in f["result"]]
    assert answers == [{"outcome": {"outcome": "cancelled"}}]
    assert c.last_turn.stop_reason == "cancelled"
    c.close()


def test_permission_requests_without_a_cancel_are_still_rejected(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake(prompts=[[{"permission": REJECT}, {"text": "done"}]])
    c = client(run, tmp_path)
    assert ask(c).choices[0].message.content == "done"
    answers = [f["result"] for f in run.responses() if isinstance(f.get("result"), dict) and "outcome" in f["result"]]
    assert answers == [{"outcome": {"outcome": "selected", "optionId": "reject"}}]
    c.close()


@pytest.mark.parametrize(
    ("script", "verdict"),
    [
        ([{"text": "1\n2\n"}, {"wait_cancel": True}], "pass"),
        ([{"text": "1\n"}, {"sleep": 30}], "fail"),
        ([{"text": "done"}], "inconclusive"),
    ],
)
def test_the_live_cancel_check_against_the_fake(
    fake: Callable[..., FakeRun], script: list[dict[str, Any]], verdict: str
) -> None:
    from ossbot.ctl import hermes_probe

    run = fake(session={"mode": "ossbot-chat"}, prompts=[script])
    doc = hermes_probe.cancel_check(v3_core, auth_method="default", api_key=None, model="claude-haiku-4.5",
                                    effort="low", fallbacks=[], spawn=run.spawn, after_s=0.5, grace_s=1.0)  # fmt: skip
    assert doc["verdict"] == verdict, doc
    if verdict == "pass":
        assert doc["stop_reason"] == "cancelled" and doc["cancel_to_response_s"] < 1.0
        assert run.requests("session/cancel")


def test_cancel_without_an_active_turn_is_a_no_op(fake: Callable[..., FakeRun], tmp_path: Path) -> None:
    run = fake()
    c = client(run, tmp_path)
    c.cancel()  # no agent yet
    assert ask(c).choices[0].message.content == "ok"
    c.cancel()  # between turns
    assert "session/cancel" not in run.methods()
    assert ask(c).choices[0].message.content == "ok"
    assert len(run.procs) == 1
    c.close()
