"""The assistant's code jobs, sandbox side (DESIGN §15.6; owner decision F007-R1-2): the ``code`` profile's
spec rules, the plan (``--network none``, no key, exec ``/tmp``), the filter rules, kill, and the real
``run_code.sh`` (caps, timeout, a bare environment) with the host python standing in for the image venv."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ossbot.dockerfilter import rules
from ossbot.runner import chat_jobs
from ossbot.sandboxd.killwatch import KillWatch
from ossbot.sandboxd.profiles import JobSpec, SpecError, validate_job
from ossbot.sandboxd.runtime.fake import FakeContainer, FakeRuntime
from tests.conftest import write_flag
from tests.sandboxharness import variants
from tests.unit.sandboxd.conftest import Sbx

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "sandbox" / "entrypoints" / "run_code.sh"


def _spec(**kw: Any) -> JobSpec:
    base: dict[str, Any] = {"run_id": "code-r1", "cid": "sys-chat", "profile": "code", "entrypoint": "run_code",
                            "args": {"language": "python", "timeout_s": 60}, "repo_mode": "none",
                            "inputs": {"main.py": "/srv/runner/main.py", "1-photo.jpg": "/srv/runner/p.jpg"},
                            "timeout_s": 90}  # fmt: skip
    base.update(kw)
    return JobSpec.model_validate(base)


def test_valid_code_spec() -> None:
    assert validate_job(_spec()).spec.profile == "code"


@pytest.mark.parametrize(
    "kw",
    [
        {"cid": "c_01ABCDEFGH"},
        {"inputs": {"1-photo.jpg": "/srv/runner/p.jpg"}},  # no source
        {"inputs": {"main.sh": "/srv/runner/m"}},  # bash source for a python job
        {"inputs": {"main.py": "/a", "main.sh": "/b"}},
        {"inputs": {"main.py": "/a", **{f"{i}-f.png": f"/f{i}" for i in range(6)}}},
        {"args": {"language": "ruby"}},
        {"args": {"language": "python", "timeout_s": 121}},
        {"args": {"language": "python", "timeout_s": 60, "network": "host"}},
        {"timeout_s": 65},  # under the code timeout + 10 s
        {"repo_mode": "rw"},
        {"entrypoint": "extract_text", "args": {}},
    ],
)
def test_code_spec_rules(kw: dict[str, Any]) -> None:
    with pytest.raises((SpecError, ValueError)):
        validate_job(_spec(**kw))


def test_code_job_runs_offline_without_key_or_tokens(sbx: Sbx) -> None:
    def handler(rt: FakeRuntime, c: FakeContainer) -> int:
        out = c.volume_path(rt, "/io/output")
        assert out is not None
        (out / "result.json").write_text(json.dumps({"ok": True, "exit_code": 0}))
        return 0

    sbx.runtime.handlers["run_code"] = handler
    spec = _spec(inputs={"main.py": sbx.runner_input("main.py", b"print(1)")})
    assert sbx.jobs.submit(spec)["state"] == "succeeded"
    argv = sbx.runtime.argvs[-1]
    assert argv[argv.index("--network") + 1] == "none" and "--read-only" in argv
    assert "OSSBOT_ARG_LANGUAGE=python" in argv and "OSSBOT_ARG_TIMEOUT_S=60" in argv
    env_names = [argv[i + 1].split("=", 1)[0] for i, a in enumerate(argv) if a in ("--env", "-e")]
    # (the generic proxy variables point at nothing on --network none; run_code.sh clears them anyway)
    assert not [n for n in env_names if "KEY" in n or "TOKEN" in n or "SECRET" in n], env_names
    assert any(a.startswith("/tmp:rw,exec") for a in argv)


def test_kill_stops_a_running_code_job(sbx: Sbx) -> None:
    spec = variants()["code"]
    sbx.runtime.volumes.update({"ossbot-in-r1-plan", "ossbot-out-r1-plan"})
    sbx.runtime.handlers["run_code"] = lambda rt, c: -1  # keeps running
    sbx.runtime.run(spec)
    sbx.jobs._containers["r1-plan"] = [spec.name]
    write_flag(sbx.settings.control_dir / "killed", {"level": "soft", "reason": "x"})
    assert KillWatch(sbx.jobs, sbx.settings.control_dir).tick() == 1
    assert ("kill", spec.name) in sbx.runtime.calls


def test_filter_treats_code_like_validate_offline() -> None:
    assert "code" in rules.NETWORK_NONE_PROFILES and "code" in rules.TMP_EXEC_PROFILES
    assert rules.CMD_BY_PROFILE["code"] == frozenset({"run_code"}) and ("code", "run_code") not in rules.KEY_PAIRS


def test_runner_rechecks_the_claim() -> None:
    import base64

    job = {"language": "python", "timeout_s": 30, "code_b64": base64.b64encode(b"print(1)").decode(),
           "files": [{"name": "1-a.png", "b64": base64.b64encode(b"png").decode()}]}  # fmt: skip
    assert chat_jobs.code_inputs(job) == ("python", 30, {"main.py": b"print(1)", "1-a.png": b"png"})
    bads = [{**job, "language": "ruby"}, {**job, "timeout_s": 500}, {**job, "files": [{"name": "../x", "b64": ""}]},
            {**job, "files": [{"name": "main.py", "b64": ""}]}]  # fmt: skip
    for bad in bads:
        with pytest.raises(ValueError):
            chat_jobs.code_inputs(bad)
    spec = chat_jobs.code_spec("code-x", "python", 30, {"main.py": "/srv/x"})
    assert spec["profile"] == "code" and spec["timeout_s"] == 60


# ------------------------------------------------------------------ the real entrypoint script
BASH = shutil.which("bash") or "/bin/bash"
needs_bash = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None or os.name == "nt",
                                reason="needs bash and jq")  # fmt: skip


Ran = tuple[int, dict[str, Any], Path]


def _run(tmp: Path, code: str, *, language: str = "python", timeout_s: int = 20,
         files: dict[str, bytes] | None = None, env: dict[str, str] | None = None) -> Ran:  # fmt: skip
    io = tmp / "io"
    shutil.rmtree(io, ignore_errors=True)
    (io / "input").mkdir(parents=True)
    (io / "input" / ("main.py" if language == "python" else "main.sh")).write_text(code)
    for name, data in (files or {}).items():
        (io / "input" / name).write_bytes(data)
    run_env = {"PATH": "/usr/bin:/bin", "OSSBOT_IO": str(io), "OSSBOT_CODE_PYTHON": sys.executable,
               "OSSBOT_WORK": str(tmp / "work"), "OSSBOT_ARG_LANGUAGE": language,
               "OSSBOT_ARG_TIMEOUT_S": str(timeout_s), **(env or {})}  # fmt: skip
    proc = subprocess.run([BASH, str(SCRIPT)], env=run_env, capture_output=True, check=False, timeout=120)
    result = json.loads((io / "output" / "result.json").read_text())
    return proc.returncode, result, io / "output"


@needs_bash
def test_script_runs_python_and_collects_results(tmp_path: Path) -> None:
    code = ("import os, pathlib\nprint(sorted(os.listdir(os.environ['OSSBOT_INPUT_DIR'])))\n"
            "pathlib.Path(os.environ['OSSBOT_OUTPUT_DIR'], 'r.csv').write_text('a,b\\n')\n")  # fmt: skip
    code_rc, result, out = _run(tmp_path, code, files={"1-photo.jpg": b"jpg"})
    assert code_rc == 0 and result["ok"] is True and result["exit_code"] == 0 and result["timed_out"] is False
    assert (out / "stdout.txt").read_text() == "['1-photo.jpg', 'main.py']\n"
    assert result["files"] == [{"name": "r.csv", "bytes": 4}] and (out / "out-r.csv").read_text() == "a,b\n"


@needs_bash
def test_script_environment_is_bare(tmp_path: Path) -> None:
    _, result, out = _run(tmp_path, "env | sort", language="bash",
                          env={"KIRO_API_KEY": "ksk_fake", "GITHUB_READ_TOKEN": "github_pat_fake",
                               "HTTPS_PROXY": "http://proxy:3128"})  # fmt: skip
    names = {line.split("=", 1)[0] for line in (out / "stdout.txt").read_text().splitlines()}
    assert result["exit_code"] == 0
    assert not names & {"KIRO_API_KEY", "GITHUB_READ_TOKEN", "HTTPS_PROXY", "OSSBOT_ARG_LANGUAGE"}, names
    assert {"PATH", "HOME", "OSSBOT_INPUT_DIR", "OSSBOT_OUTPUT_DIR"} <= names


@needs_bash
def test_script_timeout_kills_the_program(tmp_path: Path) -> None:
    _, result, _ = _run(tmp_path, "import time\nprint('start', flush=True)\ntime.sleep(60)\n", timeout_s=1)
    assert result["timed_out"] is True and result["exit_code"] in (124, 137)


@needs_bash
def test_script_caps_streams_and_result_files(tmp_path: Path) -> None:
    code = "\n".join([
        "import os, pathlib", "print('x' * 100000)", "o = pathlib.Path(os.environ['OSSBOT_OUTPUT_DIR'])",
        "[(o / f'f{i}.txt').write_text('t') for i in range(7)]", "(o / 'bad.exe').write_bytes(b'MZ')",
        "(o / 'big.png').write_bytes(b'0' * (11 * 1024 * 1024))", "os.symlink('/etc/passwd', o / 'link.txt')", "",
    ])  # fmt: skip
    _, result, out = _run(tmp_path, code)
    assert result["stdout_bytes"] == 100001 and len((out / "stdout.txt").read_bytes()) == 32768
    assert len(result["files"]) == 5 and {"bad.exe", "big.png", "link.txt"} <= set(result["dropped"])
    assert not (out / "out-link.txt").exists() and not (out / "out-bad.exe").exists()


@needs_bash
@pytest.mark.parametrize(("env", "error"), [({"OSSBOT_ARG_LANGUAGE": "ruby"}, "bad_language"),
                                            ({"OSSBOT_ARG_TIMEOUT_S": "999"}, "bad_timeout")])  # fmt: skip
def test_script_refuses_bad_arguments(tmp_path: Path, env: dict[str, str], error: str) -> None:
    rc, result, _ = _run(tmp_path, "print(1)", env=env)
    assert rc == 2 and result["error_class"] == error
