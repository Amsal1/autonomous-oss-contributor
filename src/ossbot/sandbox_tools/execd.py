#!/usr/bin/env python3
"""ossbot-execd: deterministic executor in ``agent-exec`` (DESIGN §10.4). Stdlib only; baked
into ``sandbox-full`` at /opt/ossbot/bin/ossbot-execd. No key exists in this container.

Listens on ``/run/exec/exec.sock`` (0600, uid 10001), one connection and one request at a time.
Requests are JSON lines ``{id, op: read_file|write_file|list_dir|run, args}`` (<= 2 MB, ``cmd``
<= 8 KB); responses ``{id, ok, result | error, truncated, duration_ms}``. Every request and
response (file contents cut to 4 KB) is appended to ``/io/output/exec-log.jsonl``.

Path rules for the file tools: ``path`` is relative to ``/work/repo`` or absolute under it (<= 4096
bytes, no NUL); ``realpath(dirname)`` must be under the repo; the final component is opened with
``O_NOFOLLOW`` relative to a directory fd, and ``/proc/self/fd/<fd>`` must still be under the
repo afterwards (TOCTOU). Writes into ``.git/`` and ``.kiro/`` are rejected, and so is creating
anything other than a regular file. Error codes: EPATH, ELINK, ETOOBIG, EDENIED, EINVAL.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

MAX_REQUEST = 2 * 1024 * 1024
MAX_CMD = 8 * 1024
MAX_PATH = 4096
READ_LIMIT = 256 * 1024
WRITE_LIMIT = 1024 * 1024
LIST_MAX_ENTRIES = 2000
LIST_MAX_DEPTH = 3
OUTPUT_CAP = 64 * 1024
RUN_MAX_TIMEOUT = 1200
LOG_CONTENT_CAP = 4096
DENIED_TOP = frozenset({".git", ".kiro"})
OPS = ("read_file", "write_file", "list_dir", "run")


def fixed_env(work: str = "/work") -> dict[str, str]:
    """The complete environment of every ``run`` command (env -i + this set)."""
    cache = f"{work}/cache"
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/mise/shims",
        "HOME": f"{work}/.home",
        "LANG": "C.UTF-8",
        "GOFLAGS": "-mod=mod",
        "GOPROXY": "off",
        "GOMODCACHE": f"{cache}/go/mod",
        "GOCACHE": f"{cache}/go/build",
        "npm_config_offline": "true",
        "npm_config_cache": f"{cache}/npm",
        "UV_OFFLINE": "1",
        "UV_CACHE_DIR": f"{cache}/uv",
        "PIP_NO_INDEX": "1",
        "PIP_CACHE_DIR": f"{cache}/pip",
        # Gradle runs offline through $GRADLE_USER_HOME/init.d/ossbot-offline.init.gradle and Maven through
        # ~/.m2/settings.xml, both written by exec.sh (lib/jvm_env.sh offline); GRADLE_OPTS stays unset.
        "GRADLE_USER_HOME": f"{cache}/gradle",
        "JAVA_HOME": "/usr/lib/jvm/java-21-openjdk-amd64",
        "MAVEN_USER_HOME": f"{work}/.home/.m2",
        "MAVEN_OPTS": f"-Duser.home={work}/.home",
        "MISE_DATA_DIR": f"{cache}/mise",
        "CHECKPOINT_DISABLE": "1",
        "TMPDIR": "/tmp",  # noqa: S108 - container tmpfs
    }


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


class Executor:
    def __init__(self, repo: str = "/work/repo", work: str = "/work") -> None:
        self.repo = os.path.realpath(repo)
        self.env = fixed_env(work)
        self.before_open: Callable[[str], None] = lambda _parent: None  # test hook (TOCTOU)

    # ------------------------------------------------------------------ paths
    def _split(self, path: Any) -> tuple[str, str]:
        if not isinstance(path, str) or not path or "\x00" in path or len(path.encode("utf-8", "replace")) > MAX_PATH:
            raise ToolError("EPATH", "path must be a non-empty string of at most 4096 bytes without NUL")
        full = os.path.normpath(path if os.path.isabs(path) else os.path.join(self.repo, path))
        if not _under(full, self.repo):
            raise ToolError("EPATH", "path is outside /work/repo")
        if full == self.repo:
            return self.repo, ""
        parent, name = os.path.split(full)
        real_parent = os.path.realpath(parent)
        if not _under(real_parent, self.repo):
            raise ToolError("EPATH", "parent directory resolves outside /work/repo")
        return real_parent, name

    def _fd_under_repo(self, fd: int) -> None:
        if not _under(os.readlink(f"/proc/self/fd/{fd}"), self.repo):
            raise ToolError("EPATH", "path moved outside /work/repo while opening")

    def _open_dir(self, real_parent: str) -> int:
        self.before_open(real_parent)
        try:
            dfd = os.open(real_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as exc:
            raise ToolError("EPATH", f"cannot open directory: {exc.strerror}") from exc
        try:
            self._fd_under_repo(dfd)
        except ToolError:
            os.close(dfd)
            raise
        return dfd

    def _open_final(self, dfd: int, name: str, flags: int, mode: int = 0o644) -> int:
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, mode, dir_fd=dfd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ToolError("ELINK", "final path component is a symlink") from exc
            if exc.errno in (errno.ENOENT, errno.EISDIR, errno.ENOTDIR):
                raise ToolError("EPATH", exc.strerror or "no such file") from exc
            raise ToolError("EDENIED", exc.strerror or "open failed") from exc
        try:
            self._fd_under_repo(fd)
        except ToolError:
            os.close(fd)
            raise
        return fd

    # -------------------------------------------------------------------- ops
    def read_file(self, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        real_parent, name = self._split(args.get("path"))
        if not name:
            raise ToolError("EPATH", "path is a directory")
        offset, limit = args.get("offset", 0), args.get("limit", READ_LIMIT)
        if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 0 < limit <= READ_LIMIT:
            raise ToolError("EINVAL", f"offset >= 0 and 0 < limit <= {READ_LIMIT}")
        dfd = self._open_dir(real_parent)
        try:
            fd = self._open_final(dfd, name, os.O_RDONLY)
        finally:
            os.close(dfd)
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ToolError("EDENIED", "not a regular file")
            fh.seek(offset)
            data = fh.read(limit)
        truncated = offset + len(data) < st.st_size
        return {"content": data.decode("utf-8", "replace"), "size": st.st_size, "offset": offset}, truncated

    def write_file(self, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("EINVAL", "content must be a string")
        data = content.encode("utf-8")
        if len(data) > WRITE_LIMIT:
            raise ToolError("ETOOBIG", "content exceeds 1 MB")
        real_parent, name = self._split(args.get("path"))
        if not name:
            raise ToolError("EPATH", "path is a directory")
        rel = os.path.relpath(os.path.join(real_parent, name), self.repo)
        if rel.split(os.sep, 1)[0].lower() in DENIED_TOP:
            raise ToolError("EDENIED", "writes into .git/ and .kiro/ are not allowed")
        dfd = self._open_dir(real_parent)
        try:
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                existed = True
                if stat.S_ISLNK(st.st_mode):
                    raise ToolError("ELINK", "final path component is a symlink")
                if not stat.S_ISREG(st.st_mode):
                    raise ToolError("EDENIED", "only regular files can be written")
            except FileNotFoundError:
                existed = False
            fd = self._open_final(dfd, name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        finally:
            os.close(dfd)
        with os.fdopen(fd, "wb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise ToolError("EDENIED", "only regular files can be written")
            if not existed:
                os.fchmod(fh.fileno(), 0o644)
            fh.write(data)
        return {"path": rel, "bytes": len(data), "created": not existed}, False

    def list_dir(self, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        depth = args.get("depth", 1)
        if not isinstance(depth, int) or not 1 <= depth <= LIST_MAX_DEPTH:
            raise ToolError("EINVAL", "1 <= depth <= 3")
        real_parent, name = self._split(args.get("path", "."))
        start = os.path.join(real_parent, name) if name else real_parent
        if os.path.islink(start):
            raise ToolError("ELINK", "directory is a symlink")
        if not os.path.isdir(start) or not _under(os.path.realpath(start), self.repo):
            raise ToolError("EPATH", "not a directory under /work/repo")
        entries: list[dict[str, Any]] = []
        truncated = False

        def walk(directory: str, level: int) -> None:
            nonlocal truncated
            try:
                items = sorted(os.scandir(directory), key=lambda e: e.name)
            except OSError:
                return
            for item in items:
                if len(entries) >= LIST_MAX_ENTRIES:
                    truncated = True
                    return
                if item.is_symlink():
                    kind = "link"
                elif item.is_dir(follow_symlinks=False):
                    kind = "dir"
                elif item.is_file(follow_symlinks=False):
                    kind = "file"
                else:
                    kind = "other"
                size = item.stat(follow_symlinks=False).st_size if kind == "file" else None
                entries.append({"path": os.path.relpath(item.path, self.repo), "type": kind, "size": size})
                if kind == "dir" and level < depth:
                    walk(item.path, level + 1)

        walk(start, 1)
        return {"entries": entries}, truncated

    def run(self, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        cmd, timeout = args.get("cmd"), args.get("timeout_s", 600)
        if not isinstance(cmd, str) or not cmd or len(cmd.encode("utf-8")) > MAX_CMD or "\x00" in cmd:
            raise ToolError("EINVAL", "cmd must be a non-empty string of at most 8 KB")
        if not isinstance(timeout, int) or not 1 <= timeout <= RUN_MAX_TIMEOUT:
            raise ToolError("EINVAL", "1 <= timeout_s <= 1200")
        cwd_arg = args.get("cwd", ".")
        real_parent, name = self._split(cwd_arg)
        cwd = os.path.realpath(os.path.join(real_parent, name) if name else real_parent)
        if not _under(cwd, self.repo) or not os.path.isdir(cwd):
            raise ToolError("EPATH", "cwd must be a directory under /work/repo")
        proc = subprocess.Popen(
            ["/bin/bash", "-o", "pipefail", "-lc", cmd], cwd=cwd, env=dict(self.env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )  # fmt: skip
        outputs: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        cut = {"stdout": False, "stderr": False}

        def pump(name: str, stream: Any) -> None:
            for chunk in iter(lambda: stream.read(65536), b""):
                room = OUTPUT_CAP - len(outputs[name])
                if room > 0:
                    outputs[name] += chunk[:room]
                if len(chunk) > room:
                    cut[name] = True
            stream.close()

        threads = [threading.Thread(target=pump, args=(n, getattr(proc, n)), daemon=True) for n in outputs]
        for t in threads:
            t.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)  # the whole process group
            proc.wait()
        for t in threads:
            t.join(timeout=5)
        result = {
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "stdout": outputs["stdout"].decode("utf-8", "replace"),
            "stderr": outputs["stderr"].decode("utf-8", "replace"),
        }
        return result, cut["stdout"] or cut["stderr"]

    def handle(self, request: Any) -> dict[str, Any]:
        started = time.monotonic()
        req_id = request.get("id") if isinstance(request, dict) else None
        response: dict[str, Any] = {"id": req_id, "ok": False, "truncated": False}
        try:
            if not isinstance(request, dict):
                raise ToolError("EINVAL", "request must be an object")
            op, args = request.get("op"), request.get("args", {})
            if op not in OPS or not isinstance(args, dict):
                raise ToolError("EINVAL", "unknown op or bad args")
            result, truncated = getattr(self, op)(args)
            response.update(ok=True, result=result, truncated=truncated)
        except ToolError as exc:
            response["error"] = {"code": exc.code, "message": str(exc)}
        response["duration_ms"] = int((time.monotonic() - started) * 1000)
        return response


def _log_copy(doc: Any) -> Any:
    if isinstance(doc, dict):
        return {k: _log_copy(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_log_copy(v) for v in doc]
    if isinstance(doc, str) and len(doc) > LOG_CONTENT_CAP:
        return doc[:LOG_CONTENT_CAP] + "...[truncated]"
    return doc


def bind(path: str) -> socket.socket:
    try:
        st = os.lstat(path)
        if stat.S_ISSOCK(st.st_mode):
            os.unlink(path)
        else:
            raise SystemExit(f"execd: {path} exists and is not a socket")
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    sock.listen(1)
    return sock


def serve(sock: socket.socket, executor: Executor, log_path: str | None) -> None:
    log = open(log_path, "a", encoding="utf-8") if log_path else None  # noqa: SIM115 - process lifetime
    while True:
        conn, _ = sock.accept()
        with conn, conn.makefile("rb") as reader, conn.makefile("wb") as writer:
            while True:
                line = reader.readline(MAX_REQUEST + 1)
                if not line:
                    break
                if len(line) > MAX_REQUEST:
                    response = {"id": None, "ok": False, "error": {"code": "ETOOBIG", "message": "request > 2 MB"}}
                    writer.write((json.dumps(response) + "\n").encode())
                    writer.flush()
                    break
                try:
                    request = json.loads(line)
                except ValueError:
                    request = None
                response = executor.handle(request)
                writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                writer.flush()
                if log is not None:
                    log.write(json.dumps({"request": _log_copy(request), "response": _log_copy(response)}) + "\n")
                    log.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ossbot-execd")
    parser.add_argument("--socket", default="/run/exec/exec.sock")
    parser.add_argument("--repo", default="/work/repo")
    parser.add_argument("--work", default="/work")
    parser.add_argument("--log", default="/io/output/exec-log.jsonl")
    args = parser.parse_args(argv)
    serve(bind(args.socket), Executor(args.repo, args.work), args.log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
