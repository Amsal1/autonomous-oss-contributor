"""The assistant's code jobs in the real ``sandbox-full`` image (DESIGN §15.6; owner decision F007-R1-2;
root). ``run_code`` runs in profile ``code`` through sandboxd's JobService and the Docker filter: no
network, no secrets in the environment, the wall timeout kills the program, Pillow/numpy/pandas work."""

from __future__ import annotations

import json
import secrets
from typing import Any

from tests.sandbox.conftest import Sandbox
from tests.sandbox.test_extract_text import CID


def _run(sandbox: Sandbox, code: str, *, timeout_s: int = 60, files: dict[str, bytes] | None = None
         ) -> tuple[dict[str, Any], dict[str, bytes]]:  # fmt: skip
    inputs = sandbox.root / "inputs" / secrets.token_hex(4)
    inputs.mkdir(parents=True)
    (inputs / "main.py").write_text(code)
    for name, data in (files or {}).items():
        (inputs / name).write_bytes(data)
    plan = sandbox.plan(CID, "code", entrypoint="run_code", inputs={p.name: p for p in inputs.iterdir()},
                        env={"OSSBOT_ARG_LANGUAGE": "python", "OSSBOT_ARG_TIMEOUT_S": str(timeout_s)},
                        timeout=timeout_s + 30)  # fmt: skip
    out = sandbox.run(plan)
    assert out.exit_code == 0 and out.error_class is None, (out.exit_code, out.error_class, out.logs[-2000:])
    return json.loads(out.files["result.json"]), out.files


NETWORK = """
import socket
for host, port in (("1.1.1.1", 443), ("10.212.0.2", 3141), ("proxy", 3128), ("api.github.com", 443)):
    try:
        socket.create_connection((host, port), timeout=3).close()
        print("REACHED", host)
    except OSError as exc:
        print("blocked", host, type(exc).__name__)
"""


def test_network_is_unreachable(sandbox: Sandbox) -> None:
    result, files = _run(sandbox, NETWORK)
    assert result["exit_code"] == 0
    assert b"REACHED" not in files["stdout.txt"] and files["stdout.txt"].count(b"blocked") == 4


def test_no_secrets_in_the_environment(sandbox: Sandbox) -> None:
    result, files = _run(sandbox, "import os\nprint('\\n'.join(sorted(os.environ)))\n")
    names = set(files["stdout.txt"].decode().split())
    assert result["exit_code"] == 0 and not {n for n in names if "KEY" in n or "TOKEN" in n or "PROXY" in n}


def test_timeout_kills_the_program(sandbox: Sandbox) -> None:
    result, _ = _run(sandbox, "import time\ntime.sleep(300)\n", timeout_s=3)
    assert result["timed_out"] is True


def test_image_libraries_and_result_files(sandbox: Sandbox) -> None:
    code = ("import os, PIL.Image, numpy, pandas\n"
            "img = PIL.Image.new('RGB', (64, 32))\n"
            "img.save(os.path.join(os.environ['OSSBOT_OUTPUT_DIR'], 'blank.png'))\n"
            "print(img.size, numpy.zeros(3).sum(), len(pandas.DataFrame({'a': [1, 2]})))\n")  # fmt: skip
    result, files = _run(sandbox, code)
    assert result["exit_code"] == 0, files.get("stderr.txt")
    assert files["stdout.txt"].strip() == b"(64, 32) 0.0 2"
    assert result["files"] == [{"name": "blank.png", "bytes": len(files["out-blank.png"])}]
