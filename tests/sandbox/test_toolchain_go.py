"""Go (build-env design §6.2, N6, §15, AC 12): ``.tool-versions golang 1.26.10`` is an exact pin, so the re-fetch
installs Go 1.26.10 (mise, dl.google.com, sha256-checked) and validate and the offline agent-exec run it
(``GOTOOLCHAIN=local``: Go never downloads a toolchain itself). With only the go.mod lines (``go 1.26``,
``toolchain go1.26.10``: minimums) the image's 1.27.3 satisfies them and nothing is installed. Strict egress; L-12."""

from __future__ import annotations

import time

from tests.sandbox.conftest import Sandbox
from tests.sandbox.toolchain_common import assert_l12, execd_runs, job_lines, log, prepare, tool, validate

CID = "c_TOOLCHAIN6"
MOD_CID = "c_TOOLCHAIN7"
CHECK = "go version && go run . && go env GOTOOLCHAIN"


def test_exact_go_pin_is_installed_and_used_offline(sandbox: Sandbox) -> None:
    start = time.time() - 1
    try:
        info = prepare(sandbox, CID, "go-toolchain")
        go = tool(info, "go")
        assert go["chosen"] == "1.26.10" and go["action"] == "install", info["toolchains"]
        assert_l12(job_lines(sandbox, CID, "fetch-strict", start))
        out, done = validate(sandbox, CID, [{"name": "go", "cmd": CHECK, "timeout_s": 600}])
        lines = log(out, "go").splitlines()
        assert done["go"]["exit_code"] == 0, lines
        assert lines[0].startswith("go version go1.26.10 ") and lines[1] == "go1.26.10" and lines[2] == "local"
        replies = execd_runs(sandbox, CID, [{"id": 1, "cmd": "go version && go build ./... && go run .",
                                             "timeout_s": 600}])  # fmt: skip
        result = replies["1"]["result"]
        assert result["exit_code"] == 0, result
        assert result["stdout"].splitlines()[0].startswith("go version go1.26.10 ")
    finally:
        sandbox.cleanup(CID)


def test_go_mod_minimums_use_the_image_go_and_install_nothing(sandbox: Sandbox) -> None:
    try:
        info = prepare(sandbox, MOD_CID, "go-mod-only")
        go = tool(info, "go")
        assert go["chosen"] == "1.27.3" and go["action"] == "image", info["toolchains"]
        assert not [e for e in info["toolchains"] if e.get("action") == "install"], info["toolchains"]
        out, done = validate(sandbox, MOD_CID, [{"name": "go", "cmd": "go version && go run .", "timeout_s": 300}])
        assert done["go"]["exit_code"] == 0 and log(out, "go").splitlines()[1] == "go1.27.3", log(out, "go")
        # L-4: the image's mise tools through /opt/mise/shims under execd's env -i, offline (MISE_OFFLINE=1,
        # MISE_SHARED_INSTALL_DIRS + MISE_SYSTEM_CONFIG_FILE; the findings §3.6 shim bug)
        image_tools = "go version && node --version && terraform version | head -1 && tofu version | head -1"
        replies = execd_runs(sandbox, MOD_CID, [{"id": 1, "timeout_s": 120, "cmd": image_tools}])
        result = replies["1"]["result"]
        assert result["exit_code"] == 0, (result["stdout"][-1500:], result["stderr"][-1500:])
        lines = result["stdout"].splitlines()
        assert lines[0].startswith("go version go1.27.3 ") and lines[1] == "v24.22.1", lines
        assert lines[2] == "Terraform v1.17.0" and lines[3] == "OpenTofu v1.13.1", lines
    finally:
        sandbox.cleanup(MOD_CID)
