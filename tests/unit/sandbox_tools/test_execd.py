"""ossbot-execd (DESIGN §10.4, §22.1 H1): fixed env, path rules, symlinks, TOCTOU, caps, timeouts."""

from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ossbot.sandbox_tools import execd
from ossbot.sandbox_tools.execd import Executor, ToolError


@pytest.fixture
def ex(tmp_path: Path) -> Executor:
    work = tmp_path / "work"
    repo = work / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".kiro").mkdir()
    (repo / "src" / "a.py").write_text("print(1)\n")
    (work / ".home").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET")
    os.symlink("/proc/self/environ", repo / "env")
    os.symlink(str(outside), repo / "outlink")
    os.symlink("src", repo / "srclink")
    return Executor(str(repo), str(work))


def _err(fn: Any, args: dict[str, Any]) -> str:
    with pytest.raises(ToolError) as exc:
        fn(args)
    return exc.value.code


def test_env_is_exactly_the_fixed_set(ex: Executor, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIRO_API_KEY", "ksk_should_never_leak")
    monkeypatch.setenv("OSSBOT_PARENT_CANARY", "1")
    result, _ = ex.run({"cmd": "env -0", "timeout_s": 10})
    names = {item.split("=", 1)[0] for item in result["stdout"].split("\0") if item}
    # The process starts with exactly ex.env (env -i semantics); bash itself and the login
    # profile (bash -l) add only these. Nothing from execd's own environment leaks through.
    assert set(ex.env) <= names
    assert names - set(ex.env) <= {"PWD", "SHLVL", "_", "OLDPWD", "XDG_DATA_DIRS"}
    assert ex.env["GOPROXY"] == "off" and ex.env["UV_OFFLINE"] == "1" and ex.env["PIP_NO_INDEX"] == "1"
    assert "KIRO_API_KEY" not in names


def test_read_file_ok(ex: Executor) -> None:
    result, truncated = ex.read_file({"path": "src/a.py"})
    assert result["content"] == "print(1)\n" and not truncated
    result, _ = ex.read_file({"path": os.path.join(ex.repo, "src", "a.py")})
    assert result["size"] == 9


def test_read_file_final_symlink_is_elink(ex: Executor) -> None:
    assert _err(ex.read_file, {"path": "env"}) == "ELINK"


def test_read_file_symlinked_parent_outside_is_epath(ex: Executor) -> None:
    assert _err(ex.read_file, {"path": "outlink/secret.txt"}) == "EPATH"


def test_read_file_symlinked_parent_inside_repo_ok(ex: Executor) -> None:
    result, _ = ex.read_file({"path": "srclink/a.py"})
    assert result["content"] == "print(1)\n"


@pytest.mark.parametrize(
    "path", ["../outside/secret.txt", "/etc/hostname", "/proc/self/environ", "", "a\x00b", "x" * 5000]
)
def test_path_escapes_rejected(ex: Executor, path: str) -> None:
    assert _err(ex.read_file, {"path": path}) == "EPATH"


def test_toctou_swap_caught_by_proc_fd_recheck(ex: Executor, tmp_path: Path) -> None:
    src = Path(ex.repo) / "src"

    def swap(parent: str) -> None:
        if parent == str(src):
            shutil.move(str(src), str(tmp_path / "moved"))
            os.symlink(str(tmp_path / "outside"), src)
            (tmp_path / "outside" / "a.py").write_text("SECRET")

    ex.before_open = swap
    assert _err(ex.read_file, {"path": "src/a.py"}) == "EPATH"


def test_read_caps(ex: Executor) -> None:
    (Path(ex.repo) / "big.txt").write_bytes(b"x" * (300 * 1024))
    result, truncated = ex.read_file({"path": "big.txt"})
    assert len(result["content"]) == 256 * 1024 and truncated
    assert _err(ex.read_file, {"path": "big.txt", "limit": 300 * 1024}) == "EINVAL"
    result, truncated = ex.read_file({"path": "big.txt", "offset": 256 * 1024})
    assert len(result["content"]) == 44 * 1024 and not truncated


def test_read_fifo_denied(ex: Executor) -> None:
    os.mkfifo(Path(ex.repo) / "pipe")
    assert _err(ex.read_file, {"path": "pipe"}) == "EDENIED"


def test_write_file_new_and_existing(ex: Executor) -> None:
    result, _ = ex.write_file({"path": "src/new.py", "content": "x = 1\n"})
    assert result["created"] is True
    new = Path(ex.repo) / "src" / "new.py"
    assert oct(new.stat().st_mode & 0o777) == "0o644" and new.read_text() == "x = 1\n"
    (Path(ex.repo) / "src" / "a.py").chmod(0o755)
    ex.write_file({"path": "src/a.py", "content": "print(2)\n"})
    assert oct((Path(ex.repo) / "src" / "a.py").stat().st_mode & 0o777) == "0o755"


@pytest.mark.parametrize("path", [".git/config", ".git/hooks/pre-commit", ".kiro/settings/mcp.json", ".GIT/x"])
def test_write_into_git_and_kiro_rejected(ex: Executor, path: str) -> None:
    assert _err(ex.write_file, {"path": path, "content": "x"}) in {"EDENIED", "EPATH"}


@pytest.mark.parametrize("path", ["/run/exec/exec.sock", "/io/output/x", "/io/input/x", "../outside/x"])
def test_write_outside_repo_rejected(ex: Executor, path: str) -> None:
    assert _err(ex.write_file, {"path": path, "content": "x"}) == "EPATH"


def test_write_through_symlink_rejected(ex: Executor) -> None:
    assert _err(ex.write_file, {"path": "env", "content": "x"}) == "ELINK"
    assert _err(ex.write_file, {"path": "outlink/secret.txt", "content": "x"}) == "EPATH"
    assert (Path(ex.repo).parent.parent / "outside" / "secret.txt").read_text() == "SECRET"


def test_write_non_regular_and_size(ex: Executor) -> None:
    os.mkfifo(Path(ex.repo) / "pipe")
    assert _err(ex.write_file, {"path": "pipe", "content": "x"}) == "EDENIED"
    assert _err(ex.write_file, {"path": "src", "content": "x"}) in {"EDENIED", "EPATH"}
    assert _err(ex.write_file, {"path": "big", "content": "x" * (1024 * 1024 + 1)}) == "ETOOBIG"
    assert _err(ex.write_file, {"path": "nodir/x.py", "content": "x"}) == "EPATH"  # no directory creation


def test_list_dir_reports_links_without_following(ex: Executor) -> None:
    result, _ = ex.list_dir({"path": ".", "depth": 2})
    kinds = {e["path"]: e["type"] for e in result["entries"]}
    assert kinds["env"] == "link" and kinds["outlink"] == "link" and kinds["srclink"] == "link"
    assert kinds["src"] == "dir" and kinds["src/a.py"] == "file"
    assert not any(p.startswith("outlink/") for p in kinds)
    assert _err(ex.list_dir, {"path": "outlink"}) == "ELINK"
    assert _err(ex.list_dir, {"path": ".", "depth": 4}) == "EINVAL"


def test_list_dir_entry_cap(ex: Executor) -> None:
    many = Path(ex.repo) / "many"
    many.mkdir()
    for i in range(2100):
        (many / f"f{i}").write_text("")
    result, truncated = ex.list_dir({"path": "many"})
    assert len(result["entries"]) == 2000 and truncated


def test_run_cwd_escapes_rejected(ex: Executor) -> None:
    assert _err(ex.run, {"cmd": "pwd", "cwd": ".."}) == "EPATH"
    assert _err(ex.run, {"cmd": "pwd", "cwd": "outlink"}) == "EPATH"
    result, _ = ex.run({"cmd": "pwd", "cwd": "src"})
    assert result["stdout"].strip() == os.path.join(ex.repo, "src")


def test_run_timeout_kills_process_group(ex: Executor, tmp_path: Path) -> None:
    marker = tmp_path / "survivor"
    started = time.monotonic()
    result, _ = ex.run({"cmd": f"(sleep 3; touch {marker}) & sleep 30", "timeout_s": 1})
    assert result["timed_out"] is True and time.monotonic() - started < 10
    time.sleep(3.5)
    assert not marker.exists()  # the background child died with the group


def test_run_output_caps(ex: Executor) -> None:
    result, truncated = ex.run({"cmd": "head -c 200000 /dev/zero | tr '\\0' a; echo err >&2", "timeout_s": 30})
    assert len(result["stdout"]) == 64 * 1024 and truncated
    assert result["stderr"] == "err\n"


def test_run_arg_validation(ex: Executor) -> None:
    assert _err(ex.run, {"cmd": "x" * 9000}) == "EINVAL"
    assert _err(ex.run, {"cmd": "true", "timeout_s": 1201}) == "EINVAL"


def test_handle_unknown_op_and_shapes(ex: Executor) -> None:
    assert ex.handle({"id": 1, "op": "exec", "args": {}})["error"]["code"] == "EINVAL"
    assert ex.handle("nope")["ok"] is False
    ok = ex.handle({"id": 2, "op": "read_file", "args": {"path": "src/a.py"}})
    assert ok["ok"] and ok["id"] == 2 and "duration_ms" in ok


def test_socket_server_mode_and_log(ex: Executor, tmp_path: Path) -> None:
    sock_path = str(tmp_path / "exec.sock")
    log = tmp_path / "exec-log.jsonl"
    server = execd.bind(sock_path)
    assert oct(os.stat(sock_path).st_mode & 0o777) == "0o600"
    threading.Thread(target=execd.serve, args=(server, ex, str(log)), daemon=True).start()
    client = socket.socket(socket.AF_UNIX)
    client.connect(sock_path)
    big = "y" * 10000
    client.sendall(
        (json.dumps({"id": 1, "op": "write_file", "args": {"path": "b.txt", "content": big}}) + "\n").encode()
    )
    reader = client.makefile("rb")
    assert json.loads(reader.readline())["ok"] is True
    client.close()
    for _ in range(50):
        if log.exists() and log.read_text():
            break
        time.sleep(0.05)
    entry = json.loads(log.read_text().splitlines()[0])
    assert len(entry["request"]["args"]["content"]) < 5000  # file contents truncated to 4 KB in the log
