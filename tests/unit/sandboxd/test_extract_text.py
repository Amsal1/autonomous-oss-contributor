"""PDF text for the assistant (DESIGN §14.12.5, §14.19 item 9; PLAN item 37): the ``extract`` profile's
spec rules, the job plan (``--network none``, no key, no repo) and the real ``extract_text.sh`` with a
stand-in ``pdftotext``."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ossbot.dockerfilter import rules
from ossbot.sandboxd.jobs import ApiError
from ossbot.sandboxd.profiles import JobSpec, SpecError, validate_job
from ossbot.sandboxd.runtime.fake import FakeContainer, FakeRuntime
from tests.unit.sandboxd.conftest import Sbx

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "sandbox" / "entrypoints" / "extract_text.sh"


def _spec(**kw: Any) -> JobSpec:
    base: dict[str, Any] = {"run_id": "extract-r1", "cid": "sys-chat", "profile": "extract",
                            "entrypoint": "extract_text", "args": {}, "repo_mode": "none",
                            "inputs": {"doc.pdf": "/srv/runner/doc.pdf"}, "timeout_s": 120}  # fmt: skip
    base.update(kw)
    return JobSpec.model_validate(base)


def test_valid_extract_spec() -> None:
    assert validate_job(_spec()).spec.profile == "extract"


@pytest.mark.parametrize(
    "kw",
    [
        {"cid": "c_01ABCDEFGH"},  # only the pseudo-cid sys-chat
        {"inputs": {}},
        {"inputs": {"doc.pdf": "/srv/runner/a", "x.txt": "/srv/runner/b"}},
        {"inputs": {"other.pdf": "/srv/runner/a"}},
        {"repo_mode": "rw"},
        {"timeout_s": 301},
        {"args": {"page": 1}},
        {"entrypoint": "kiro"},
    ],
)
def test_extract_spec_rules(kw: dict[str, Any]) -> None:
    with pytest.raises((SpecError, ValueError)):
        validate_job(_spec(**kw))


def test_sys_chat_is_only_for_extract() -> None:
    with pytest.raises((SpecError, ValueError)):
        validate_job(_spec(profile="fetch", entrypoint="fetch", args={"repo": "o/r"}, repo_mode="rw", inputs={}))


def test_extract_job_runs_offline_without_a_key(sbx: Sbx) -> None:
    def handler(rt: FakeRuntime, c: FakeContainer) -> int:
        inp, out = c.volume_path(rt, "/io/input"), c.volume_path(rt, "/io/output")
        assert inp is not None and out is not None
        assert (inp / "doc.pdf").read_bytes().startswith(b"%PDF-")
        (out / "text.txt").write_bytes(b"page text")
        (out / "result.json").write_text('{"ok":true,"error_class":null,"bytes":9,"truncated":false}')
        return 0

    sbx.runtime.handlers["extract_text"] = handler
    spec = _spec(inputs={"doc.pdf": sbx.runner_input("doc.pdf", b"%PDF-1.4 test")})
    view = sbx.jobs.submit(spec)
    assert view["state"] == "succeeded", view
    argv = sbx.runtime.argvs[-1]
    assert argv[argv.index("--network") + 1] == "none"
    assert not any(a.startswith("KIRO_API_KEY") for a in argv) and "/work" not in " ".join(argv)
    text = sbx.settings.artifacts_dir / "sys-chat" / "runs" / "extract-r1" / "text.txt"
    assert text.read_bytes() == b"page text"


def test_extract_job_refused_while_killed(sbx: Sbx) -> None:
    (sbx.settings.control_dir / "killed").write_text(json.dumps({"level": "soft", "reason": "x"}))
    with pytest.raises(ApiError):
        sbx.jobs.submit(_spec(inputs={"doc.pdf": sbx.runner_input("doc.pdf", b"%PDF-1.4")}))


def test_filter_binds_extract_to_network_none_and_its_entrypoint() -> None:
    assert "extract" in rules.NETWORK_NONE_PROFILES and rules.CMD_BY_PROFILE["extract"] == frozenset({"extract_text"})
    assert ("extract", "extract_text") not in rules.KEY_PAIRS


# ------------------------------------------------------------------ the real entrypoint script
BASH = shutil.which("bash")
pytestmark_script = pytest.mark.skipif(BASH is None or os.name == "nt", reason="needs bash")


def _fake_pdftotext(tmp: Path, body: str) -> Path:
    path = tmp / "pdftotext"
    path.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _run(tmp: Path, pdf: bytes | None, tool: Path, max_bytes: int = 2097152) -> tuple[int, dict[str, Any], bytes]:
    io = tmp / "io"
    shutil.rmtree(io, ignore_errors=True)
    (io / "input").mkdir(parents=True)
    if pdf is not None:
        (io / "input" / "doc.pdf").write_bytes(pdf)
    env = {"PATH": "/usr/bin:/bin", "OSSBOT_IO": str(io), "OSSBOT_PDFTOTEXT": str(tool),
           "OSSBOT_TEXT_MAX": str(max_bytes)}  # fmt: skip
    proc = subprocess.run([BASH or "bash", str(SCRIPT)], env=env, capture_output=True, check=False, timeout=60)
    result = json.loads((io / "output" / "result.json").read_text())
    text_path = io / "output" / "text.txt"
    return proc.returncode, result, text_path.read_bytes() if text_path.exists() else b""


@pytestmark_script
def test_script_writes_the_text(tmp_path: Path) -> None:
    tool = _fake_pdftotext(tmp_path, 'printf "Hello from page one\\n"')
    code, result, text = _run(tmp_path, b"%PDF-1.4 x", tool)
    assert code == 0 and result == {"ok": True, "error_class": None, "bytes": 20, "truncated": False}
    assert text == b"Hello from page one\n"


@pytestmark_script
def test_script_truncates_at_the_cap(tmp_path: Path) -> None:
    tool = _fake_pdftotext(tmp_path, "head -c 5000 /dev/zero | tr '\\0' 'a'")
    code, result, text = _run(tmp_path, b"%PDF-1.4 x", tool, max_bytes=1024)
    assert code == 0 and result["truncated"] is True and result["bytes"] == 1024 and text == b"a" * 1024


@pytestmark_script
def test_script_handles_an_endless_producer(tmp_path: Path) -> None:
    tool = _fake_pdftotext(tmp_path, "yes abcdefgh")  # SIGPIPE once head has its bytes
    code, result, text = _run(tmp_path, b"%PDF-1.4 x", tool, max_bytes=4096)
    assert code == 0 and result["truncated"] is True and len(text) == 4096


@pytestmark_script
@pytest.mark.parametrize(("pdf", "error"), [(None, "no_input"), (b"PK\x03\x04 not a pdf", "not_pdf")])
def test_script_refuses_bad_input(tmp_path: Path, pdf: bytes | None, error: str) -> None:
    tool = _fake_pdftotext(tmp_path, "exit 99")
    code, result, _ = _run(tmp_path, pdf, tool)
    assert code == 2 and result["ok"] is False and result["error_class"] == error


@pytestmark_script
def test_script_reports_a_failing_pdftotext(tmp_path: Path) -> None:
    tool = _fake_pdftotext(tmp_path, "echo damaged >&2; exit 1")
    code, result, text = _run(tmp_path, b"%PDF-1.4 broken", tool)
    assert code == 1 and result["error_class"] == "pdftotext_exit_1" and text == b""


@pytestmark_script
def test_script_refuses_a_symlinked_input(tmp_path: Path) -> None:
    tool = _fake_pdftotext(tmp_path, "cat")
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"%PDF-1.4 secret")
    io = tmp_path / "io"
    (io / "input").mkdir(parents=True)
    (io / "input" / "doc.pdf").symlink_to(secret)
    env = {"PATH": "/usr/bin:/bin", "OSSBOT_IO": str(io), "OSSBOT_PDFTOTEXT": str(tool)}
    proc = subprocess.run([BASH or "bash", str(SCRIPT)], env=env, capture_output=True, check=False, timeout=60)
    assert proc.returncode == 2
    assert json.loads((io / "output" / "result.json").read_text())["error_class"] == "no_input"
