"""Python from ``requires-python >=3.11,<3.12`` (build-env design §6.2, §15, AC 12, L-5): the re-fetch installs a
3.11 with uv through the GitHub mirror (``UV_PYTHON_INSTALL_MIRROR``, github_read hosts) into the task cache, and
the venv fetch creates runs on it in validate and in the offline agent-exec. The same validate job runs a build
script whose own curl ignores the proxy variables (AC 12: proxy-unaware tools, netshim). Strict egress."""

from __future__ import annotations

import json

from tests.sandbox.conftest import Sandbox
from tests.sandbox.test_jvm_offline import fixture_tar
from tests.sandbox.toolchain_common import execd_runs, log, prepare, tool, validate

CID = "c_TOOLCHAIN5"
VERSION = "python -c 'import sys; print(sys.version.split()[0])'"


def test_requires_python_311_and_a_proxy_unaware_build_script(sandbox: Sandbox) -> None:
    try:
        info = prepare(sandbox, CID, "python-requires")
        py = tool(info, "python")
        assert py["chosen"] == ">=3.11,<3.12" and py["action"] == "install", info["toolchains"]
        assert info["prefetch"]["venv"] == 0 and info["prefetch"]["pip"] == 0, info
        out, done = validate(sandbox, CID, [
            {"name": "version", "cmd": VERSION, "timeout_s": 60},
            {"name": "import", "cmd": "python -c 'import fixture; print(fixture.VERSION)'", "timeout_s": 60},
            {"name": "build-script", "cmd": "tar -xf /io/input/script.tar -C /tmp && sh /tmp/build.sh",
             "timeout_s": 120},
        ], inputs={"script.tar": fixture_tar("proxy-unaware-build-script")})  # fmt: skip
        for name in ("version", "import", "build-script"):
            assert done[name]["exit_code"] == 0, (name, log(out, name)[-2000:])
        assert log(out, "version").strip().startswith("3.11."), log(out, "version")
        assert log(out, "import").strip() == "(3, 11)"
        assert int(log(out, "build-script").strip().splitlines()[-1]) > 0  # pip's simple index via netshim
        replies = execd_runs(sandbox, CID, [{"id": 1, "cmd": f"/work/venv/bin/{VERSION}", "timeout_s": 60}])
        assert replies["1"]["result"]["exit_code"] == 0, replies["1"]
        assert replies["1"]["result"]["stdout"].strip().startswith("3.11."), json.dumps(replies["1"])[-1000:]
    finally:
        sandbox.cleanup(CID)
