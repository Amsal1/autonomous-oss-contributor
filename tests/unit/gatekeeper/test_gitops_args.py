"""Every gatekeeper git call keeps protocol.file.allow=never except the two bundle fetches (R4-M1),
which read only gatekeeper-built paths; /package refuses a foreign artifact_dir (§12.5, §22.1)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ossbot.common.paths import Paths
from ossbot.common.schemas import State
from ossbot.gatekeeper import gitops
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.github_writer import push_argv
from ossbot.gatekeeper.gitops import (
    ASKPASS_SCRIPT,
    GIT_ARGS,
    Git,
    effective_config,
    git_env,
    https_fetch,
    install_askpass,
)
from tests.conftest import insert_contribution
from tests.githarness import GitWorld, package_request, stage_artifacts
from tests.unit.gatekeeper.test_package_verify import VETTED, package_ok, post


class DryGit(Git):
    """Records argv instead of running git."""

    def run(  # type: ignore[override]
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        git_args: Sequence[str] = GIT_ARGS,
        stdin: bytes | None = None,
        check: bool = True,
        pass_fds: Sequence[int] = (),
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append([gitops.GIT_BIN, *git_args, *args])
        return subprocess.CompletedProcess([], 0, b"", b"")


def _assert_hardened(argv: list[str]) -> None:
    assert effective_config(argv, "core.hooksPath") == "/dev/null"
    assert effective_config(argv, "core.fsmonitor") == "false"
    assert effective_config(argv, "transfer.fsckObjects") == "true"
    assert effective_config(argv, "http.proxy") == "http://10.212.0.2:3150"


async def test_every_call_never_except_bundle_fetch(gctx: AppContext, world: GitWorld) -> None:
    cid = insert_contribution(gctx, State.PACKAGING, vetting_json=VETTED)
    out = world.package(world.base, {"src/app.py": "print('args')\n"})
    await package_ok(gctx, world, cid, out)
    calls = gctx.gitops.git.calls
    assert len(calls) > 10
    bundle_fetches = []
    for argv in calls:
        _assert_hardened(argv)
        value = effective_config(argv, "protocol.file.allow")
        if value == "always":
            bundle_fetches.append(argv)
        else:
            assert value == "never", argv
    assert len(bundle_fetches) == 1
    (fetch,) = bundle_fetches
    assert "fetch" in fetch
    assert "--no-recurse-submodules" in fetch
    source = next(a for a in fetch if a.endswith(".bundle"))
    assert source == str(gctx.gitops.work_bundle(cid, 1))
    assert Path(source).is_relative_to(gctx.paths.gatekeeper_dir)


def test_https_fetch_argv(tmp_path: Path) -> None:
    git = DryGit(home=tmp_path)
    https_fetch(git, tmp_path, "https://github.com/langfuse/langfuse.git", "+refs/heads/main:refs/upstream/main")
    (argv,) = git.calls
    assert effective_config(argv, "protocol.file.allow") == "never"
    _assert_hardened(argv)
    assert argv[-2:] == ["https://github.com/langfuse/langfuse.git", "+refs/heads/main:refs/upstream/main"]
    for bad in ("/srv/ossbot/artifacts/x", "file:///etc", "ext::sh -c id", "http://github.com/a/b.git"):
        with pytest.raises(ValueError, match=r"github\.com"):
            https_fetch(git, tmp_path, bad, "refs/heads/main")


def test_fetch_bundle_refuses_paths_outside_gatekeeper(paths: Paths, tmp_path: Path) -> None:
    ops = gitops.GitOps(paths, DryGit(home=tmp_path))
    with pytest.raises(ValueError, match="gatekeeper-private"):
        ops.fetch_bundle(tmp_path, paths.artifacts_dir / "c_1" / "1" / "change.bundle", "a", "b")
    with pytest.raises(ValueError, match="gatekeeper-private"):
        ops.fetch_bundle(tmp_path, Path("relative.bundle"), "a", "b")


def test_git_env_inherits_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/evil")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath=/tmp/hooks'")
    monkeypatch.setenv("HTTPS_PROXY", "http://evil:1")
    env = git_env(tmp_path)
    assert set(env) == {"PATH", "LC_ALL", *gitops.GIT_ENV}
    assert env["HOME"] == str(tmp_path)


def test_push_argv_has_no_secret_and_no_force() -> None:
    argv = push_argv("test-bot-login/langfuse", "test-bot-login", "a" * 40, "ossbot/1-fix-abc123")
    assert argv[-2:] == [
        "https://test-bot-login@github.com/test-bot-login/langfuse.git",
        f"{'a' * 40}:refs/heads/ossbot/1-fix-abc123",
    ]
    assert "--force" not in argv
    assert not any(a.startswith("+") for a in argv)
    assert "--no-verify" in argv
    assert effective_config(argv, "credential.helper") == ""
    with pytest.raises(ValueError, match="bad push"):
        push_argv("test-bot-login/langfuse", "test-bot-login", "HEAD", "x")
    with pytest.raises(ValueError, match="bad push"):
        push_argv("test-bot-login/langfuse", "test-bot-login", "a" * 40, "../x")


def test_askpass_reads_token_from_inherited_memfd(tmp_path: Path) -> None:
    script = install_askpass(tmp_path / "githome")
    assert script.read_text() == ASKPASS_SCRIPT
    assert script.stat().st_mode & 0o777 == 0o700
    fd = os.memfd_create("t", os.MFD_CLOEXEC)
    try:
        os.write(fd, b"ghp_faketoken")
        os.set_inheritable(fd, True)
        env = {"PATH": "/usr/bin:/bin", "OSSBOT_ASKPASS_FD": str(fd), "OSSBOT_GIT_USER": "test-bot-login"}
        for _ in range(2):  # git may ask twice; a memfd re-reads from the start each time
            pw = subprocess.run([str(script), "Password for 'https://x@github.com': "], env=env, pass_fds=(fd,),
                                capture_output=True, check=True)  # fmt: skip
            assert pw.stdout == b"ghp_faketoken"
        user = subprocess.run([str(script), "Username for 'https://github.com': "], env=env, pass_fds=(fd,),
                              capture_output=True, check=True)  # fmt: skip
        assert user.stdout == b"test-bot-login\n"
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "artifact_dir",
    [
        "{root}/{other}/1",
        "{root}/{cid}/2",
        "{root}/{cid}/1/../../{other}/1",
        "/tmp/{cid}/1",
        "{root}/{cid}/1/change.bundle",
    ],
)
async def test_artifact_dir_must_be_gatekeeper_built(gctx: AppContext, world: GitWorld, artifact_dir: str) -> None:
    out = world.package(world.base, {"src/app.py": "print('dir')\n"})
    cid = insert_contribution(gctx, State.PACKAGING, vetting_json=VETTED)
    other = insert_contribution(gctx, State.PACKAGING, vetting_json=VETTED)
    sums = stage_artifacts(gctx.paths, cid, 1, out.out)
    body: dict[str, Any] = package_request(gctx.paths, cid, 1, sums, out.head, world.base)
    body["artifact_dir"] = artifact_dir.format(root=gctx.paths.artifacts_dir, cid=cid, other=other)
    resp = await post(gctx, cid, body)
    assert resp.status_code == 400
    assert resp.json()["error"] == "artifact_dir"
    assert not gctx.gitops.git.calls  # refused before any git ran
