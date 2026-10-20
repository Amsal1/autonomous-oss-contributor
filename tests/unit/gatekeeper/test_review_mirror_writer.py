"""``review_mirror_writer`` (ADR-0019 C, U5; AC 24): bound to the mirror repo, force only on its own refs."""

from __future__ import annotations

from pathlib import Path

import pytest

from ossbot.gatekeeper.gitops import Git
from ossbot.gatekeeper.review_mirror_writer import MirrorWriter, MirrorWriteRefused, push_argv
from tests.fakes.fake_github import FakeGitHub

MIRROR = "test-bot-login/ossbot-review"
CID = "c_01JABCDEFG"
H, B = "a" * 40, "b" * 40


def _writer(gh: FakeGitHub, tmp_path: Path) -> MirrorWriter:
    return MirrorWriter(gh.client("github_pat_fake_mirror"), Git(home=tmp_path / "githome"), "github_pat_fake_mirror",
                        "test-bot-login", MIRROR)  # fmt: skip


def test_push_goes_only_to_the_mirror_url_with_forced_display_refs() -> None:
    argv = push_argv(MIRROR, "test-bot-login", H, B, CID)
    urls = [a for a in argv if a.startswith("https://")]
    assert urls == [f"https://test-bot-login@github.com/{MIRROR}.git"]
    assert argv[-2:] == [f"+{H}:refs/heads/review/{CID}", f"+{B}:refs/heads/review-base/{CID}"]
    assert "--force" in argv and "--no-verify" in argv
    assert not [a for a in argv if "github_pat" in a]  # the token is never in argv


@pytest.mark.parametrize(("head", "base", "cid"), [("HEAD", B, CID), (H, "main", CID), (H, B, "../x"),
                                                   (H, B, "c_01JABCDEFG:refs/heads/main")])  # fmt: skip
def test_bad_push_targets_are_refused(head: str, base: str, cid: str) -> None:
    with pytest.raises(MirrorWriteRefused):
        push_argv(MIRROR, "test-bot-login", head, base, cid)


async def test_api_paths_outside_the_mirror_are_refused(tmp_path: Path) -> None:
    gh = FakeGitHub()
    w = _writer(gh, tmp_path)
    for path in ("/repos/langfuse/langfuse/pulls", "/repos/test-bot-login/ossbot-review-2/pulls",
                 "/repos/test-bot-login/ossbot-review/../../langfuse/langfuse/pulls", "/user/repos"):  # fmt: skip
        with pytest.raises(MirrorWriteRefused):
            await w._send("POST", path, {})
    assert gh.writes == []


async def test_open_update_and_close_stay_in_the_mirror(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.add_repo(MIRROR, private=True, visibility="private")
    w = _writer(gh, tmp_path)
    first = await w.open_or_update(CID, "[c r1] t", "body")
    again = await w.open_or_update(CID, "[c r2] t", "body 2")
    assert first.number == again.number
    assert gh.pulls[MIRROR.lower()][0]["title"] == "[c r2] t"
    await w.close(first.number, CID)
    assert gh.pulls[MIRROR.lower()][0]["state"] == "closed"
    assert gh.writes and all(r.path.startswith(f"/repos/{MIRROR}/") for r in gh.writes)
    methods = [r.method for r in gh.writes]
    assert methods == ["POST", "PATCH", "PATCH", "DELETE", "DELETE"]
