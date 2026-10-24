"""Node from ``.nvmrc`` and pnpm from ``packageManager`` (build-env design §6.2, §15, AC 12): the re-fetch installs
Node 22 (the exact version from nodejs.org's index, mise) and pnpm 12.15.0 through that Node's corepack into the task
cache; validate and the offline agent-exec use both. Strict egress; L-12 on the re-fetch's squid lines."""

from __future__ import annotations

import time

from tests.sandbox.conftest import Sandbox
from tests.sandbox.toolchain_common import assert_l12, execd_runs, job_lines, log, prepare, tool, validate

CID = "c_TOOLCHAIN4"
CHECK = "node --version && pnpm --version && node test.js"


def test_nvmrc_22_and_pnpm_through_corepack_also_offline(sandbox: Sandbox) -> None:
    start = time.time() - 1
    try:
        info = prepare(sandbox, CID, "node-nvmrc-pnpm")
        node = tool(info, "node")
        assert node["chosen"].startswith("22.") and node["action"] == "install", info["toolchains"]
        pnpm = tool(info, "pnpm")
        assert pnpm["chosen"] == "pnpm@12.15.0" and pnpm["action"] == "install", info["toolchains"]
        assert_l12(job_lines(sandbox, CID, "fetch-strict", start))
        out, done = validate(sandbox, CID, [{"name": "check", "cmd": CHECK, "timeout_s": 300}])
        lines = log(out, "check").splitlines()
        assert done["check"]["exit_code"] == 0, lines
        assert (
            lines[0] == f"v{node['chosen']}" and lines[1] == "12.15.0" and lines[2] == lines[0].replace("v", "node=v")
        )
        replies = execd_runs(sandbox, CID, [{"id": 1, "cmd": CHECK, "timeout_s": 300}])
        result = replies["1"]["result"]
        assert result["exit_code"] == 0, result
        assert result["stdout"].splitlines()[:2] == [f"v{node['chosen']}", "12.15.0"]
    finally:
        sandbox.cleanup(CID)
