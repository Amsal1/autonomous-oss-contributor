"""Rust from ``rust-toolchain.toml`` (build-env design §6.2, F1, §15, AC 12): channel 1.98.0 with rustfmt is
installed by rustup (static.rust-lang.org, hashes checked by rustup) into the task's RUSTUP_HOME at the re-fetch,
next to the image's 1.99.1 linked as ``ossbot-1.99.1``; validate and the offline agent-exec build with it
(``RUSTUP_AUTO_INSTALL=0`` offline). Strict egress."""

from __future__ import annotations

from tests.sandbox.conftest import Sandbox
from tests.sandbox.toolchain_common import execd_runs, log, prepare, tool, validate

CID = "c_TOOLCHAIN8"
CHECK = "rustc --version && cargo fmt --version && cargo build --offline && ./target/debug/fixture"


def test_rust_toolchain_file_channel_is_installed_and_used_offline(sandbox: Sandbox) -> None:
    try:
        info = prepare(sandbox, CID, "rust-toolchain")
        rust = tool(info, "rust")
        assert rust["chosen"] == "1.98.0" and rust["action"] == "install", info["toolchains"]
        assert info["prefetch"]["cargo"] == 0, info
        out, done = validate(sandbox, CID, [{"name": "rust", "cmd": CHECK, "timeout_s": 600}])
        lines = log(out, "rust").splitlines()
        assert done["rust"]["exit_code"] == 0, lines[-40:]
        assert lines[0].startswith("rustc 1.98.0 ") and lines[-1] == "fixture", lines[-10:]
        replies = execd_runs(sandbox, CID, [{"id": 1, "cmd": f"cargo clean && {CHECK}", "timeout_s": 600},
                                            {"id": 2, "cmd": "echo $RUSTUP_TOOLCHAIN $RUSTUP_AUTO_INSTALL",
                                             "timeout_s": 30}])  # fmt: skip
        result = replies["1"]["result"]
        assert result["exit_code"] == 0, (result["stdout"][-2000:], result["stderr"][-2000:])
        assert result["stdout"].splitlines()[0].startswith("rustc 1.98.0 ")
        assert replies["2"]["result"]["stdout"].split() == ["1.98.0", "0"]
    finally:
        sandbox.cleanup(CID)
