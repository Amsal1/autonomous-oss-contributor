"""Validate (DESIGN §9.9): the plan's commands and smoke runs offline in profile ``validate``.

IMPLEMENTED -> VALIDATING -> VALIDATED | VALIDATION_FAILED. A fixed first command records the diff
against the round base for the reviewer (``ossbot-diff``; data only). Operator imports have no
plan: the per-ecosystem defaults run. A failure stores ``validation_sig`` (diff + failing checks) and
the previous one, so implement can stop a retry that changed nothing (``validation_stuck``).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ossbot.runner.pipeline import StageContext, StageRun
from ossbot.runner.stages.common import artifact, artifact_json, job, target, write_inputs

STAGE = "validate"
DIFF_CMD = (
    "git -c core.hooksPath=/dev/null -c core.fsmonitor=false add -A --intent-to-add"
    " && git -c core.quotePath=true -c diff.renames=false diff --no-ext-diff --no-textconv --no-color {base}"
)
DEFAULTS: dict[str, list[tuple[str, str]]] = {
    "go": [("go-vet", "go vet ./..."), ("go-test", "go test ./...")],
    "node": [("npm-test", "npm test --if-present")],
    "python": [("pytest", "python -m pytest -q")],
    "terraform": [("tofu-validate", "tofu validate")],
    "helm": [("helm-lint", "helm lint .")],
    "rust": [("cargo-test", "cargo test --offline")],
}


def commands(plan: dict[str, Any], ecosystems: list[str], base: str) -> list[dict[str, Any]]:
    out = [{"name": "ossbot-diff", "cmd": DIFF_CMD.format(base=base), "timeout_s": 120}]
    planned = [*plan.get("validation_commands", []), *plan.get("runtime_smoke", [])]
    if planned:
        out += [{"name": str(cmd["name"]).lower()[:64], "cmd": str(cmd["cmd"]), "timeout_s": 1200} for cmd in planned]
    else:
        for eco in ecosystems:
            out += [{"name": name, "cmd": cmd, "timeout_s": 1200} for name, cmd in DEFAULTS.get(eco, [])]
    return out[:50]


def failure_sig(diff: str, checks: list[dict[str, Any]]) -> str:
    """sha256 of the diff and the set of failing (name, exit_code): equal only when a retry changed nothing."""
    failing = sorted({(str(cmd.get("name")), str(cmd.get("exit_code"))) for cmd in checks if cmd.get("exit_code") != 0})
    digest = hashlib.sha256(diff.encode("utf-8", errors="replace"))
    digest.update(b"\0" + json.dumps(failing).encode())
    return digest.hexdigest()


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid = str(c["id"])
    if c["state"] == "IMPLEMENTED":
        sc.transition(cid, "IMPLEMENTED", "VALIDATING")
    notes = sc.load(cid)
    plan = json.loads(c["plan_json"]) if c.get("plan_json") else {}
    base = str(c.get("head_sha") if c.get("pr_number") else c.get("base_sha") or "HEAD")
    cmds = commands(plan, list(notes.get("fetch", {}).get("ecosystems", [])), base)
    with StageRun(sc, cid, STAGE, profile="validate"):
        egress = target(sc, str(c["upstream"])).egress  # the contribution's upstream (ValidateArgs has no repo)
        inputs = write_inputs(sc, cid, STAGE, {"validate.json": json.dumps({"commands": cmds})})
        done = job(sc, cid, STAGE, "validate", "validate", {"plan_file": "validate.json"}, "rw", inputs=inputs,
                   egress=egress)  # fmt: skip
        result = artifact_json(done, "validation.json")
        diff: str | None
        try:
            diff = artifact(done, "logs/ossbot-diff.log").read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - the reviewer simply gets no diff (and no stuck check)
            diff = None
    checks = [cmd for cmd in result.get("commands", []) if cmd.get("name") != "ossbot-diff"]
    ok = all(cmd.get("exit_code") == 0 for cmd in checks)
    sig = None if ok or diff is None else failure_sig(diff, checks)  # implement compares it (validation_stuck)
    prev = None if sig is None else notes.get("validation_sig")
    sc.save(cid, validation={"commands": checks}, diff=(diff or "")[:400_000], validation_sig=sig,
            validation_prev_sig=prev)  # fmt: skip
    summary = {"passed": sum(1 for cmd in checks if cmd.get("exit_code") == 0), "total": len(checks)}
    sc.transition(cid, "VALIDATING", "VALIDATED" if ok else "VALIDATION_FAILED", {"validation": summary})
