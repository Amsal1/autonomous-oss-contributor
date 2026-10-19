"""The root half of the skill gate (DESIGN §15.5; PLAN item 36): records are data, links and special
files are refused, and activation writes exactly the bound bytes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ossbot.ctl import skills_root as sr

SKILL = "---\nname: protein-log\ndescription: log protein\nallowed-tools: [memory, ossbot_notify]\n---\nSteps.\n"


def _dirs(tmp_path: Path) -> dict[str, Path]:
    d = {n: tmp_path / n for n in ("pending", "active", "store", "control")}
    for p in d.values():
        p.mkdir(mode=0o755)
    return d


def _record(d: dict[str, Path], pid: str, payload: dict, **extra: object) -> None:
    rec = {
        "id": pid,
        "subsystem": "skills",
        "action": payload.get("action"),
        "summary": "s",
        "origin": "foreground",
        "created_at": 1_800_000_000,
        "payload": payload,
        **extra,
    }
    (d["pending"] / f"{pid}.json").write_text(json.dumps(rec), encoding="utf-8")


def _scan(d: dict[str, Path], tier: str = "self_improve") -> dict:
    return sr.scan(tier, pending_dir=d["pending"], active_dir=d["active"], store=d["store"])


def _apply(d: dict[str, Path]) -> list[dict]:
    return sr.apply_requests(
        control_dir=d["control"],
        store=d["store"],
        pending_dir=d["pending"],
        active_dir=d["active"],
        uid=os.getuid(),
        gid=os.getgid(),
    )


def _request(d: dict[str, Path], review: dict, op: str = "activate", digest: str | None = None) -> None:
    doc = {
        "op": op,
        "review_id": review["review_id"],
        "approval_id": "s_TEST000001",
        "content_sha256": digest or review["content_sha256"],
    }
    (d["control"] / "skill-request-s_TEST000001.json").write_text(json.dumps(doc), encoding="utf-8")


def test_create_is_staged_hashed_and_activated_exactly(tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    _record(d, "abcd1234", {"action": "create", "name": "protein-log", "category": "health", "content": SKILL})
    doc = _scan(d)
    assert doc["invalid"] == []
    (review,) = doc["reviews"]
    assert review["action"] == "create" and review["rel_dir"] == "health/protein-log"
    assert review["declared_tools"] == ["memory", "ossbot_notify"]
    assert review["files"] == {"SKILL.md": sr.file_hashes({"SKILL.md": SKILL.encode()})["SKILL.md"]}
    assert "+Steps." in review["diff"]
    assert not (d["active"] / "health").exists()  # staged is not active
    assert json.loads((d["store"] / "pending.json").read_text())["reviews"][0]["review_id"] == review["review_id"]
    _request(d, review)
    (done,) = _apply(d)
    assert done["result"] == "activated", done
    active = d["active"] / "health" / "protein-log" / "SKILL.md"
    assert active.read_bytes() == SKILL.encode()
    assert oct(active.stat().st_mode & 0o777) == "0o640"
    assert oct((d["active"] / "health" / "protein-log").stat().st_mode & 0o777) == "0o750"
    assert not (d["pending"] / "abcd1234.json").exists() and not list(d["control"].iterdir())
    assert sr.drift(active_dir=d["active"], store=d["store"], uid=os.getuid()) == []


def test_activation_refuses_a_digest_other_than_the_bound_one(tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    _record(d, "abcd1234", {"action": "create", "name": "x", "content": SKILL})
    (review,) = _scan(d)["reviews"]
    _request(d, review, digest="0" * 64)
    (done,) = _apply(d)
    assert done["result"] == "failed" and "digest" in done["reason"]
    assert not (d["active"] / "x").exists()


def test_tampered_staged_bytes_are_refused(tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    _record(d, "abcd1234", {"action": "create", "name": "x", "content": SKILL})
    (review,) = _scan(d)["reviews"]
    (d["store"] / "staged" / review["review_id"] / "files" / "SKILL.md").write_text("evil", encoding="utf-8")
    _request(d, review)
    (done,) = _apply(d)
    assert done["result"] == "failed" and "manifest" in done["reason"]
    assert not (d["active"] / "x").exists()


def test_update_patch_write_remove_and_delete(tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    _record(d, "aaaa0001", {"action": "create", "name": "x", "content": SKILL})
    (review,) = _scan(d)["reviews"]
    _request(d, review)
    _apply(d)
    batch = {
        "name": "x",
        "operations": [
            {"action": "patch", "name": "x", "old_string": "Steps.", "new_string": "Steps v2."},
            {"action": "write_file", "name": "x", "file_path": "references/notes.md", "file_content": "n"},
        ],
    }
    _record(d, "aaaa0002", batch)
    (review,) = _scan(d)["reviews"]
    assert review["action"] == "update" and set(review["files"]) == {"SKILL.md", "references/notes.md"}
    assert (d["active"] / "x" / "SKILL.md").read_bytes() == SKILL.encode()  # not active before the approval
    _request(d, review)
    _apply(d)
    assert b"Steps v2." in (d["active"] / "x" / "SKILL.md").read_bytes()
    assert (d["active"] / "x" / "references" / "notes.md").read_text() == "n"
    _record(d, "aaaa0003", {"action": "remove_file", "name": "x", "file_path": "references/notes.md"})
    (review,) = _scan(d)["reviews"]
    _request(d, review)
    _apply(d)
    assert not (d["active"] / "x" / "references" / "notes.md").exists()
    _record(d, "aaaa0004", {"action": "delete", "name": "x"})
    (review,) = _scan(d)["reviews"]
    assert review["action"] == "delete" and review["files"] == {}
    _request(d, review)
    _apply(d)
    assert not (d["active"] / "x").exists()
    assert sr.drift(active_dir=d["active"], store=d["store"], uid=os.getuid()) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "write_file", "name": "x", "file_path": "../escape", "file_content": "a"},
        {"action": "write_file", "name": "x", "file_path": "/etc/passwd", "file_content": "a"},
        {"action": "create", "name": "../x", "content": "a"},
        {"action": "create", "name": "x", "content": "a" * (sr.MAX_FILE + 1)},
        {"action": "terminal", "name": "x"},
        {"action": "patch", "name": "nope", "old_string": "a", "new_string": "b"},
        {"action": "remove_file", "name": "x", "file_path": "SKILL.md"},
    ],
)
def test_bad_records_are_invalid_not_staged(tmp_path: Path, payload: dict) -> None:
    d = _dirs(tmp_path)
    (d["active"] / "x").mkdir()
    (d["active"] / "x" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    _record(d, "bbbb0001", payload)
    doc = _scan(d)
    assert doc["reviews"] == [] and len(doc["invalid"]) == 1


def test_links_and_special_files_are_refused(tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    target = tmp_path / "secret.json"
    target.write_text(json.dumps({"subsystem": "skills", "payload": {"action": "create", "name": "y", "content": "a"}}))
    (d["pending"] / "cccc0001.json").symlink_to(target)  # a link record is never followed
    os.mkfifo(d["pending"] / "cccc0002.json")
    doc = _scan(d)
    assert doc["reviews"] == [] and len(doc["invalid"]) == 2
    linked = tmp_path / "linked-pending"
    linked.symlink_to(d["pending"])
    with pytest.raises((sr.StagingError, OSError)):
        sr.scan("self_improve", pending_dir=linked, active_dir=d["active"], store=d["store"])
    (d["active"] / "z").mkdir()
    (d["active"] / "z" / "SKILL.md").symlink_to(tmp_path / "secret.json")
    _record(d, "cccc0003", {"action": "edit", "name": "z", "content": "b"})
    assert len(_scan(d)["invalid"]) == 3


def test_drift_detects_unapproved_and_changed_skills(tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    _record(d, "dddd0001", {"action": "create", "name": "x", "content": SKILL})
    (review,) = _scan(d)["reviews"]
    _request(d, review)
    _apply(d)
    (d["active"] / "x" / "SKILL.md").chmod(0o644)
    (d["active"] / "x" / "SKILL.md").write_text("changed", encoding="utf-8")
    (d["active"] / "rogue").mkdir()
    (d["active"] / "rogue" / "SKILL.md").write_text("r", encoding="utf-8")
    problems = sr.drift(active_dir=d["active"], store=d["store"], uid=os.getuid())
    assert any("x: differs" in p for p in problems) and any("rogue: active without an approval" in p for p in problems)


def test_declared_tools_parsing() -> None:
    assert sr.declared_tools(b"---\ntools:\n  - terminal\n  - memory\n---\nx") == ["memory", "terminal"]
    assert sr.declared_tools(b"---\nallowed-tools: web_search, read_file\n---\n") == ["read_file", "web_search"]
    assert sr.declared_tools(b"no frontmatter") == []
