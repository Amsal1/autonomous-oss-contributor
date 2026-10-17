"""Local git world for package/unbundle/import tests: a fake upstream, a fake fork, the real
``sandbox/entrypoints/*.sh`` and a ``RemoteFetch`` that maps https://github.com URLs to local repos.

The harness's own git calls use a permissive test config (it plays GitHub and the sandbox); the
gatekeeper under test always runs with the production ``GIT_ENV``/``GIT_ARGS``/``BUNDLE_FETCH_ARGS``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ossbot.common import gitcmd
from ossbot.common.hashing import sha256_hex
from ossbot.common.paths import Paths
from ossbot.gatekeeper.gitops import Git, GitError

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SH = REPO_ROOT / "sandbox" / "entrypoints" / "package.sh"
UNBUNDLE_SH = REPO_ROOT / "sandbox" / "entrypoints" / "unbundle.sh"
NUMSTAT2JSON = REPO_ROOT / "src" / "ossbot" / "sandbox_tools" / "numstat2json.py"
NAME = "Test Person"
EMAIL = "test.person@example.org"
IDENTITY = f"{NAME} <{EMAIL}>"
PACKAGE_TS = "2026-10-17T10:00:00Z"
DISCLOSURE = (
    "\n\n---\n<details><summary>🤖 AI assistance disclosure</summary>\n\nPrepared with an AI agent.\n</details>\n"
)


def harness_env(home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Upstream Dev",
        "GIT_AUTHOR_EMAIL": "dev@upstream.example",
        "GIT_COMMITTER_NAME": "Upstream Dev",
        "GIT_COMMITTER_EMAIL": "dev@upstream.example",
        "LC_ALL": "C",
    }


def tgit(cwd: Path, *args: str, env: Mapping[str, str] | None = None, check: bool = True) -> str:
    base = harness_env(cwd.parent if cwd.exists() else cwd)
    if env:
        base.update(env)
    proc = subprocess.run(
        ["/usr/bin/git", "-c", "protocol.file.allow=always", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        env=base,
        capture_output=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.decode()}")
    return proc.stdout.decode().strip()


@dataclass
class LocalRemote:
    """``RemoteFetch`` stand-in: https://github.com/<full>.git -> a local repository."""

    repos: dict[str, Path] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, git: Git, git_dir: Path, url: str, refspec: str) -> None:
        self.calls.append((url, refspec))
        prefix, suffix = "https://github.com/", ".git"
        full = url[len(prefix) : -len(suffix)] if url.startswith(prefix) and url.endswith(suffix) else url
        path = self.repos.get(full.lower())
        if path is None:
            raise GitError(["fetch", url], 128, b"fatal: repository not found")
        proc = subprocess.run(
            ["/usr/bin/git", "-c", "protocol.file.allow=always", "fetch", "-q", "--no-tags", str(path), refspec],
            cwd=git_dir,
            env=harness_env(git_dir.parent),
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise GitError(["fetch", url, refspec], proc.returncode, proc.stderr)


@dataclass
class PackageOut:
    head: str
    out: Path
    result: dict[str, object]


class GitWorld:
    """Upstream ``langfuse/langfuse`` (main), the owner's fork, and a sandbox work tree."""

    def __init__(self, root: Path, remote: LocalRemote, upstream: str = "langfuse/langfuse") -> None:
        self.root = root
        self.remote = remote
        self.upstream_name = upstream
        root.mkdir(parents=True, exist_ok=True)
        self.upstream = root / "upstream"
        self.upstream.mkdir()
        tgit(self.upstream, "init", "-q")
        self.write(self.upstream, {"README.md": "hello\n", "src/app.py": "print('a')\n" * 3})
        self.base = self.commit(self.upstream, "initial")
        remote.repos[upstream.lower()] = self.upstream
        self.fork = root / "fork.git"
        tgit(root, "init", "-q", "--bare", str(self.fork))
        remote.repos[f"test-bot-login/{upstream.split('/', 1)[1]}".lower()] = self.fork
        self.bin = root / "bin"
        self.bin.mkdir()
        wrapper = self.bin / "numstat2json"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{NUMSTAT2JSON}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        self.work = root / "work"
        self.io = root / "io"

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def write(repo: Path, files: Mapping[str, str | None]) -> None:
        for name, content in files.items():
            path = repo / name
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    @staticmethod
    def commit(repo: Path, message: str, env: Mapping[str, str] | None = None) -> str:
        tgit(repo, "add", "-A")
        tgit(repo, "commit", "-q", "--allow-empty", "-m", message, env=env)
        return tgit(repo, "rev-parse", "HEAD")

    def advance_upstream(self, files: Mapping[str, str | None] | None = None) -> str:
        self.write(self.upstream, files or {"CHANGELOG.md": f"upstream {len(list(self.upstream.iterdir()))}\n"})
        return self.commit(self.upstream, "upstream moves on")

    def force_push_upstream(self) -> str:
        """Rewrite upstream main so that the old history (and the base) is gone."""
        tree = tgit(self.upstream, "write-tree")
        orphan = tgit(self.upstream, "commit-tree", tree, "-m", "rewritten history")
        tgit(self.upstream, "update-ref", "refs/heads/main", orphan)
        return orphan

    def sandbox_repo(self, base: str) -> Path:
        """``/work/repo`` as fetch leaves it: a clone detached at ``base``."""
        repo = self.work / "repo"
        if repo.exists():
            shutil.rmtree(repo)
        self.work.mkdir(parents=True, exist_ok=True)
        tgit(self.work, "clone", "-q", str(self.upstream), str(repo))
        tgit(repo, "checkout", "-q", "--detach", base)
        return repo

    def package(
        self,
        round_base: str,
        edits: Mapping[str, str | None],
        *,
        message: str = "fix: handle empty spans\n\nExplain the change.\n",
        name: str = NAME,
        email: str = EMAIL,
        repo_setup: bool = True,
    ) -> PackageOut:
        """Edit the sandbox tree and run the real package.sh."""
        repo = self.work / "repo"
        if repo_setup and not repo.exists():
            self.sandbox_repo(round_base)
        self.write(repo, edits)
        for sub in ("input", "output"):
            d = self.io / sub
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True)
        (self.io / "input" / "commit_message.txt").write_text(message, encoding="utf-8")
        proc = run_script(
            PACKAGE_SH,
            ["--round-base", round_base, "--package-ts", PACKAGE_TS, "--name", name, "--email", email],
            self,
        )
        result = json.loads((self.io / "output" / "result.json").read_text())
        if proc.returncode != 0:
            raise AssertionError(f"package.sh failed: {proc.stderr.decode()} {result}")
        return PackageOut(str(result["head_sha"]), self.io / "output", result)

    def push_fork(self, bundle: Path, sha: str, branch: str) -> None:
        """Play the earlier successful submit: the fork branch points at ``sha``."""
        tgit(self.fork, "fetch", "-q", str(self.upstream), "+refs/heads/main:refs/heads/main")
        tgit(self.fork, "fetch", "-q", str(bundle), f"{gitcmd.CHANGE_REF}:refs/heads/{branch}")
        assert tgit(self.fork, "rev-parse", f"refs/heads/{branch}") == sha


def run_script(script: Path, args: list[str], world: GitWorld) -> subprocess.CompletedProcess[bytes]:
    env = {
        "PATH": "/usr/bin:/bin",
        "OSSBOT_FORMAT_PATCH": gitcmd.FORMAT_PATCH,
        "OSSBOT_NUMSTAT": gitcmd.NUMSTAT,
        "OSSBOT_WORK": str(world.work),
        "OSSBOT_IO": str(world.io),
        "OSSBOT_PKG_ROOT": str(world.root / "pkgroot"),
        "OSSBOT_NUMSTAT2JSON": str(world.bin / "numstat2json"),
        "OSSBOT_UNBUNDLE_HOME": str(world.root / "unbundle-home"),
    }
    return subprocess.run(["/bin/bash", str(script), *args], env=env, capture_output=True, check=False)


def stage_artifacts(paths: Paths, cid: str, rev: int, out: Path) -> dict[str, str]:
    """sandboxd's step 5: copy the outputs to artifacts/<cid>/<rev>/ and report their sha256s."""
    dest = paths.artifacts_dir / cid / str(rev)
    dest.mkdir(parents=True, exist_ok=True)
    sums: dict[str, str] = {}
    for name in ("change.bundle", "change.patch", "diffstat.json"):
        src = out / name
        if src.exists():
            shutil.copy(src, dest / name)
            sums[name] = sha256_hex((dest / name).read_bytes())
        else:
            sums[name] = "0" * 64
    return sums


def package_request(
    paths: Paths,
    cid: str,
    rev: int,
    sums: dict[str, str],
    head: str,
    base: str,
    *,
    title: str | None = "fix(web): handle empty spans",
    body: str | None = None,
) -> dict[str, object]:
    return {
        "rev": rev,
        "artifact_dir": str(paths.artifacts_dir / cid / str(rev)),
        "sha256s": sums,
        "pr_title": title,
        "pr_body": body if body is not None else f"Fixes #1.{DISCLOSURE}<!-- ossbot:cid={cid} -->",
        "claimed_head_sha": head,
        "base_ref": "main",
        "base_sha": base,
        "validation": {"passed": True},
        "review": {"verdict": "pass"},
    }
