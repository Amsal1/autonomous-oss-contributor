"""Gatekeeper git plumbing (DESIGN §12.5 mirror strategy, R4-M1, R5-N1, I6, I12).

* Every invocation runs ``/usr/bin/git`` with ``GIT_ENV`` (no system/global config) and
  ``GIT_ARGS`` (``protocol.file.allow=never``, no hooks, no fsmonitor, fsck on transfer, proxy
  3150). The only exception is the bundle fetch, which uses ``BUNDLE_FETCH_ARGS`` (the scoped
  ``protocol.file.allow=always``, last ``-c`` wins) on a path the gatekeeper built itself.
* The gatekeeper never checks out a tree: caches and mirrors are bare repos.
* Per-upstream cache ``cache/<owner>__<repo>.git`` holds only ``refs/upstream/<branch>``; per-rev
  mirrors ``mirrors/<owner>__<repo>/<cid>-r<rev>.git`` borrow its objects via alternates and hold
  only their own refs (M13).
* R5-N1: bundles are read from the ossbox-owned artifact dir component by component with
  ``O_NOFOLLOW``, must be regular files <= 50 MB, and are copied into the gatekeeper-private
  ``work/<cid>-r<rev>.bundle`` (0600). Every bundle command runs on the copy only.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ossbot.common.hashing import sha256_hex
from ossbot.common.paths import Paths

GIT_BIN = "/usr/bin/git"
GITHUB_PROXY = "http://10.212.0.2:3150"
DEFAULT_GIT_HOME = "/srv/ossbot/gatekeeper/githome"
GIT_ENV: dict[str, str] = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": DEFAULT_GIT_HOME,
    "GIT_ATTR_NOSYSTEM": "1",
}
GIT_ARGS: list[str] = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.file.allow=never",
    "-c", "core.fsmonitor=false",
    "-c", "transfer.fsckObjects=true",
    "-c", f"http.proxy={GITHUB_PROXY}",
]  # fmt: skip
# R4-M1: git applies the `file` transport check to bundle paths; the override is scoped to the
# two bundle fetches only (the /package fetch and the import validation fetch).
BUNDLE_FETCH_ARGS: list[str] = [*GIT_ARGS, "-c", "protocol.file.allow=always"]
DIFF_SAFETY: tuple[str, ...] = ("--no-ext-diff", "--no-textconv", "--no-color")

MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_SIDE_ARTIFACT_BYTES = 20 * 1024 * 1024
BUNDLE_NAME = "change.bundle"
UPSTREAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
REF_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HEAD_LINE = re.compile(r"^([0-9a-f]{40}) (\S+)$")


class GitError(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: bytes) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr.decode("utf-8", "replace")[-2000:]
        super().__init__(f"git {' '.join(self.argv[-6:])} failed ({returncode}): {self.stderr.strip()[-300:]}")


class ArtifactError(RuntimeError):
    """The artifact file is missing, not a regular file, too large or reached via a symlink."""


def git_env(home: Path | str = DEFAULT_GIT_HOME) -> dict[str, str]:
    """A fresh environment: nothing is inherited from the service (no GIT_DIR, no proxies)."""
    return {"PATH": "/usr/bin:/bin", "LC_ALL": "C", **GIT_ENV, "HOME": str(home)}


def effective_config(argv: Sequence[str], key: str) -> str | None:
    """Value of ``-c key=...`` as git sees it (the last occurrence wins)."""
    value: str | None = None
    for index, item in enumerate(argv[:-1]):
        if item == "-c" and argv[index + 1].startswith(f"{key}="):
            value = argv[index + 1].split("=", 1)[1]
    return value


@dataclass
class Git:
    """Runs git with the hardened environment. ``calls`` records every argv (tests, audit)."""

    home: Path
    record: bool = False
    calls: list[list[str]] = field(default_factory=list)
    timeout: float = 300.0

    def run(
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
        argv = [GIT_BIN, *git_args, *args]
        if self.record:
            self.calls.append(argv)
        env = git_env(self.home)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=stdin,
            capture_output=True,
            check=False,
            timeout=self.timeout,
            pass_fds=tuple(pass_fds),
        )
        if check and proc.returncode != 0:
            raise GitError(argv, proc.returncode, proc.stderr)
        return proc

    def out(self, args: Sequence[str], *, cwd: Path, git_args: Sequence[str] = GIT_ARGS) -> str:
        return self.run(args, cwd=cwd, git_args=git_args).stdout.decode("utf-8", "replace").strip()

    def ok(self, args: Sequence[str], *, cwd: Path) -> bool:
        return self.run(args, cwd=cwd, check=False).returncode == 0


class RemoteFetch(Protocol):
    """Fetch ``refspec`` from a GitHub URL into the bare repo ``git_dir``."""

    def __call__(self, git: Git, git_dir: Path, url: str, refspec: str) -> None: ...


def https_fetch(git: Git, git_dir: Path, url: str, refspec: str) -> None:
    """Production remote fetch: https only, through proxy 3150, with the hardened GIT_ARGS."""
    if not url.startswith("https://github.com/"):
        raise ValueError("remote fetches go to https://github.com only")
    git.run(["fetch", "-q", "--no-tags", "--no-recurse-submodules", url, refspec], cwd=git_dir)


def github_url(full_name: str) -> str:
    if not UPSTREAM_RE.match(full_name):
        raise ValueError(f"bad repository name {full_name!r}")
    return f"https://github.com/{full_name}.git"


ASKPASS_SCRIPT = """#!/bin/sh
# ossbot askpass: username from the URL, password from an inherited memfd (never argv/env).
case "$1" in
  Username*) printf '%s\\n' "$OSSBOT_GIT_USER" ;;
  *) cat "/proc/self/fd/$OSSBOT_ASKPASS_FD" ;;
esac
"""


def install_askpass(home: Path) -> Path:
    """The askpass helper of both writers (``github_writer``, ``review_mirror_writer``)."""
    home.mkdir(parents=True, exist_ok=True)
    path = home / "askpass.sh"
    tmp = home / f".askpass.sh.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(ASKPASS_SCRIPT)
    os.chmod(tmp, stat.S_IRWXU)
    os.replace(tmp, path)
    return path


def push_with_token(git: Git, token: str, login: str, argv: Sequence[str], *, cwd: Path) -> None:
    """Run a push argv with the token on an inherited memfd read by askpass (never argv, URL or env)."""
    askpass = install_askpass(git.home)
    fd = os.memfd_create("ossbot-askpass", os.MFD_CLOEXEC)
    try:
        os.write(fd, token.encode("utf-8"))
        os.set_inheritable(fd, True)
        env = {"GIT_ASKPASS": str(askpass), "OSSBOT_ASKPASS_FD": str(fd), "OSSBOT_GIT_USER": login}
        git.run(argv, cwd=cwd, pass_fds=(fd,), extra_env=env)
    finally:
        os.close(fd)


def repo_key(upstream: str) -> str:
    if not UPSTREAM_RE.match(upstream):
        raise ValueError(f"bad upstream {upstream!r}")
    owner, name = upstream.split("/", 1)
    return f"{owner}__{name}"


def _write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_artifact(root: Path, parts: Sequence[str], max_bytes: int) -> bytes:
    """Open ``root/parts...`` without following any symlink (R5-N1); regular file, size-capped."""
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(root, flags_dir)
    except OSError as exc:
        raise ArtifactError(f"artifact root unavailable: {exc.strerror}") from exc
    try:
        for name in parts[:-1]:
            if name in {"", ".", ".."} or "/" in name:
                raise ArtifactError("bad path component")
            try:
                nxt = os.open(name, flags_dir, dir_fd=fd)
            except OSError as exc:
                raise ArtifactError(f"{name}: {exc.strerror}") from exc
            os.close(fd)
            fd = nxt
        leaf = parts[-1]
        if leaf in {"", ".", ".."} or "/" in leaf:
            raise ArtifactError("bad path component")
        try:
            ffd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except OSError as exc:
            raise ArtifactError(f"{leaf}: {exc.strerror}") from exc
        try:
            st = os.fstat(ffd)
            if not stat.S_ISREG(st.st_mode):
                raise ArtifactError(f"{leaf} is not a regular file")
            if st.st_size > max_bytes:
                raise ArtifactError(f"{leaf} is larger than {max_bytes} bytes")
            with os.fdopen(ffd, "rb", closefd=False) as fh:
                data = fh.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ArtifactError(f"{leaf} is larger than {max_bytes} bytes")
            return data
        finally:
            os.close(ffd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class BundleCopy:
    path: Path
    sha256: str
    size: int


def bundle_prerequisites(data: bytes) -> set[str]:
    """Prerequisite SHAs from a v2/v3 bundle header (lines starting with ``-``)."""
    end = data.find(b"\n\n")
    if not data.startswith((b"# v2 git bundle\n", b"# v3 git bundle\n")) or end < 0:
        raise ValueError("not a git bundle")
    prereqs: set[str] = set()
    for line in data[:end].split(b"\n")[1:]:
        if line.startswith(b"-"):
            sha = line[1:41].decode("ascii", "replace")
            if not SHA_RE.match(sha):
                raise ValueError("bad prerequisite line")
            prereqs.add(sha)
    return prereqs


def parse_heads(text: str) -> list[tuple[str, str]]:
    heads: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _HEAD_LINE.match(line.strip())
        if match is None:
            raise ValueError(f"unparseable head line {line[:120]!r}")
        heads.append((match.group(1), match.group(2)))
    return heads


class GitOps:
    def __init__(self, paths: Paths, git: Git | None = None, remote: RemoteFetch | None = None) -> None:
        self.paths = paths
        self.git = git or Git(home=paths.gatekeeper_dir / "githome")
        self.remote: RemoteFetch = remote or https_fetch

    # ------------------------------------------------------------------ layout
    @property
    def root(self) -> Path:
        return self.paths.gatekeeper_dir

    def cache_dir(self, upstream: str) -> Path:
        return self.root / "cache" / f"{repo_key(upstream)}.git"

    def mirror_dir(self, upstream: str, cid: str, rev: int) -> Path:
        return self.root / "mirrors" / repo_key(upstream) / f"{cid}-r{rev}.git"

    def work_bundle(self, cid: str, rev: int) -> Path:
        return self.root / "work" / f"{cid}-r{rev}.bundle"

    def derived_dir(self, cid: str, rev: int) -> Path:
        return self.root / "derived" / cid / str(rev)

    def import_dir(self, cid: str) -> Path:
        return self.root / "imports" / cid

    def artifact_dir(self, cid: str, rev: int) -> Path:
        return self.paths.artifacts_dir / cid / str(rev)

    def init_bare(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git.run(["init", "-q", "--bare", "--object-format=sha1", str(path)], cwd=path.parent)

    # ------------------------------------------------------------------ cache
    def fetch_upstream(self, upstream: str, base_ref: str, live: Path | None = None) -> str:
        """Incremental single-branch fetch into the cache; returns the tip SHA.

        With ``live`` (a per-rev mirror) the tip is also written to ``refs/upstream-live/<base_ref>``
        there (Confirm/submit ancestry check, M12); ``refs/upstream/*`` of the mirror never changes.
        """
        if not REF_NAME_RE.match(base_ref) or ".." in base_ref:
            raise ValueError(f"bad base_ref {base_ref!r}")
        cache = self.cache_dir(upstream)
        self.git.home.mkdir(parents=True, exist_ok=True)
        if not (cache / "HEAD").exists():
            self.init_bare(cache)
        self.remote(self.git, cache, github_url(upstream), f"+refs/heads/{base_ref}:refs/upstream/{base_ref}")
        tip = self.git.out(["rev-parse", "--verify", f"refs/upstream/{base_ref}^{{commit}}"], cwd=cache)
        if live is not None:
            self.git.run(["update-ref", f"refs/upstream-live/{base_ref}", tip], cwd=live)
        return tip

    # ----------------------------------------------------------------- mirrors
    def create_mirror(self, upstream: str, cid: str, rev: int, base_ref: str, tip: str) -> Path:
        mirror = self.mirror_dir(upstream, cid, rev)
        if mirror.exists():
            shutil.rmtree(mirror)
        self.init_bare(mirror)
        alternates = mirror / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(f"{self.cache_dir(upstream) / 'objects'}\n", encoding="utf-8")
        self.git.run(["update-ref", f"refs/upstream/{base_ref}", tip], cwd=mirror)
        return mirror

    def fetch_fork(self, mirror: Path, fork_repo: str, branch: str) -> str | None:
        """push_update: the fork branch into ``refs/fork/<branch>`` of the per-rev mirror (M9)."""
        if not REF_NAME_RE.match(branch) or ".." in branch:
            raise ValueError(f"bad branch {branch!r}")
        self.remote(self.git, mirror, github_url(fork_repo), f"+refs/heads/{branch}:refs/fork/{branch}")
        return self.rev_parse(mirror, f"refs/fork/{branch}")

    # ----------------------------------------------------------------- bundles
    def copy_bundle(self, cid: str, rev: int) -> BundleCopy:
        """R5-N1: O_NOFOLLOW read from artifacts/<cid>/<rev>/, private 0600 copy, hash of the copy."""
        data = read_artifact(self.paths.artifacts_dir, [cid, str(rev), BUNDLE_NAME], MAX_BUNDLE_BYTES)
        dest = self.work_bundle(cid, rev)
        _write_private(dest, data)
        return BundleCopy(dest, sha256_hex(dest.read_bytes()), len(data))

    def store_bundle_bytes(self, dest: Path, data: bytes) -> BundleCopy:
        _write_private(dest, data)
        return BundleCopy(dest, sha256_hex(dest.read_bytes()), len(data))

    def read_side_artifact(self, cid: str, rev: int, name: str) -> bytes:
        return read_artifact(self.paths.artifacts_dir, [cid, str(rev), name], MAX_SIDE_ARTIFACT_BYTES)

    def list_heads(self, repo: Path, bundle: Path) -> list[tuple[str, str]]:
        return parse_heads(self.git.out(["bundle", "list-heads", str(bundle)], cwd=repo))

    def verify_bundle(self, repo: Path, bundle: Path) -> bool:
        return self.git.ok(["bundle", "verify", "-q", str(bundle)], cwd=repo)

    def fetch_bundle(self, repo: Path, bundle: Path, src_ref: str, dst_ref: str) -> None:
        """The only git call with the scoped protocol.file.allow=always (R4-M1), on the private copy."""
        if not bundle.is_absolute() or not bundle.is_relative_to(self.root):
            raise ValueError("bundle fetches only read gatekeeper-private copies")
        self.git.run(
            ["fetch", "-q", "--no-tags", "--no-recurse-submodules", str(bundle), f"{src_ref}:{dst_ref}"],
            cwd=repo,
            git_args=BUNDLE_FETCH_ARGS,
        )

    # --------------------------------------------------------------- queries
    def rev_parse(self, repo: Path, rev: str) -> str | None:
        proc = self.git.run(["rev-parse", "-q", "--verify", f"{rev}^{{commit}}"], cwd=repo, check=False)
        value = proc.stdout.decode("ascii", "replace").strip()
        return value if proc.returncode == 0 and SHA_RE.match(value) else None

    def is_ancestor(self, repo: Path, ancestor: str, descendant: str) -> bool:
        return self.git.ok(["merge-base", "--is-ancestor", ancestor, descendant], cwd=repo)

    def rev_count(self, repo: Path, base: str, head: str) -> int:
        return int(self.git.out(["rev-list", "--count", f"{base}..{head}"], cwd=repo))

    def commit_exists(self, repo: Path, sha: str) -> bool:
        return self.git.ok(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo)

    # -------------------------------------------------------------- retention
    def delete_rev(self, upstream: str, cid: str, rev: int) -> None:
        for path in (self.mirror_dir(upstream, cid, rev), self.derived_dir(cid, rev)):
            if path.exists():
                shutil.rmtree(path)
        self.work_bundle(cid, rev).unlink(missing_ok=True)

    def revs_on_disk(self, upstream: str, cid: str) -> set[int]:
        base = self.root / "mirrors" / repo_key(upstream)
        revs: set[int] = set()
        if base.is_dir():
            for entry in base.iterdir():
                match = re.fullmatch(rf"{re.escape(cid)}-r(\d+)\.git", entry.name)
                if match:
                    revs.add(int(match.group(1)))
        return revs

    def prune(self, upstream: str, cid: str, keep: Callable[[int], bool]) -> list[int]:
        """Delete every per-rev mirror/derived/work copy of ``cid`` whose rev is not kept."""
        removed = []
        for rev in sorted(self.revs_on_disk(upstream, cid)):
            if not keep(rev):
                self.delete_rev(upstream, cid, rev)
                removed.append(rev)
        return removed
