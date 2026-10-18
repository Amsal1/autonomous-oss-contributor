"""One Kiro role invocation through sandboxd (DESIGN §15.3, §16.1).

Model and effort are resolved fresh from ``config/models.yaml`` against the gatekeeper's catalog
(catalog-or-forced, fallbacks; ``model_unavailable`` sends nothing to sandboxd). The task prompt and
its inputs are written into the job's input dir under ``/srv/ossbot/runner/inputs``. Output is the
last fenced ```json block, validated; an invalid answer gets exactly one repair retry, a second
failure fails the stage.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ossbot.common.models import Catalog, ModelsFile, ModelUnavailable, ResolvedModel
from ossbot.runner import output_parse, prompts
from ossbot.runner.errors import StageFailed, sandbox_run
from ossbot.runner.gk_client import Gatekeeper
from ossbot.runner.sbx_client import Sandbox

__all__ = ["ROLE_PROFILE", "Kiro", "LlmResult", "StageFailed", "as_prompt", "new_run_id"]
ROLE_PROFILE: dict[str, tuple[str, str]] = {  # role -> (sandboxd profile, repo_mode)
    "vetter": ("research", "none"), "scout": ("research", "snapshot"), "planner": ("research", "snapshot"),
    "reviewer": ("research", "snapshot"), "summarizer": ("research", "none"), "coder": ("coder", "rw"),
}  # fmt: skip
Check = Callable[[Any], str | None]  # deterministic check of a parsed answer: None = fine, else the reason


def new_run_id(stage: str) -> str:
    return f"{stage[:10]}-{secrets.token_hex(6)}"


@dataclass
class LlmResult[T: BaseModel]:
    value: T
    model: ResolvedModel
    model_actual: str | None
    model_mismatch: bool
    run_ids: list[str] = field(default_factory=list)
    artifact_dir: Path | None = None


class Kiro:
    def __init__(
        self,
        gk: Gatekeeper,
        sbx: Sandbox,
        models: ModelsFile,
        inputs_root: Path,
        tick: Callable[[], None] | None = None,
    ) -> None:
        self.gk = gk
        self.sbx = sbx
        self.models = models
        self.inputs_root = inputs_root
        self.tick = tick

    def resolve(self, role: str) -> ResolvedModel:
        try:
            return self.models.resolve(role, Catalog.from_rows(self.gk.catalog()))
        except ModelUnavailable as exc:
            raise StageFailed("model_unavailable", str(exc)) from exc

    def _write_inputs(self, cid: str, run_id: str, files: Mapping[str, str | bytes]) -> dict[str, str]:
        base = self.inputs_root / cid / run_id
        base.mkdir(parents=True, exist_ok=True)
        out: dict[str, str] = {}
        for name, data in files.items():
            path = base / name
            if isinstance(data, bytes):
                path.write_bytes(data)
            else:
                path.write_text(data, encoding="utf-8")
            out[name] = str(path)
        return out

    def _once(self, cid: str, role: str, model: ResolvedModel, text: str, files: Mapping[str, str | bytes],
              timeout_s: int) -> tuple[output_parse.KiroOutput, str, Path | None]:  # fmt: skip
        run_id = new_run_id(role)
        prompt_file = f"{role}-task.md"
        inputs = self._write_inputs(cid, run_id, {**files, prompt_file: text})
        profile, repo_mode = ROLE_PROFILE[role]
        spec: dict[str, Any] = {
            "run_id": run_id, "cid": cid, "profile": profile, "entrypoint": "kiro", "repo_mode": repo_mode,
            "args": {"role": role, "prompt_file": prompt_file, "model": model.model, "effort": model.effort},
            "inputs": inputs, "timeout_s": timeout_s,
        }  # fmt: skip
        job = sandbox_run(self.sbx, spec, self.tick)
        if job.get("state") != "succeeded":
            raise StageFailed(str(job.get("error_class") or "kiro_failed"), f"{role} job {run_id}")
        artifact = Path(str(job["artifact_dir"])) if job.get("artifact_dir") else None
        events = (artifact / f"{role}.events.jsonl").read_bytes() if artifact else b""
        stderr_path = artifact / f"{role}.stderr" if artifact else None
        stderr = stderr_path.read_bytes() if stderr_path is not None and stderr_path.is_file() else b""
        return output_parse.read_events(events, stderr), run_id, artifact

    def invoke[T: BaseModel](
        self,
        cid: str,
        role: str,
        out_model: type[T],
        text: str,
        files: Mapping[str, str | bytes] | None = None,
        *,
        timeout_s: int = 1200,
        schema_hint: str = "",
        check: Check | None = None,
    ) -> LlmResult[T]:
        """``check`` adds a deterministic rule on top of the schema; a violation gets the same
        single repair retry as an invalid answer."""
        model = self.resolve(role)
        files = files or {}

        def parse(answer: str) -> T:
            value = output_parse.parse_model(answer, out_model)
            reason = check(value) if check is not None else None
            if reason is not None:
                raise output_parse.OutputInvalid(reason)
            return value

        output, run_id, artifact = self._once(cid, role, model, text, files, timeout_s)
        runs = [run_id]
        try:
            value = parse(output.text)
        except output_parse.OutputInvalid as exc:  # one repair retry, then the stage fails
            repair = text + "\n\n" + output_parse.repair_prompt(exc.reason, schema_hint)
            output, run_id, artifact = self._once(cid, role, model, repair, files, timeout_s)
            runs.append(run_id)
            try:
                value = parse(output.text)
            except output_parse.OutputInvalid as again:
                raise StageFailed("output_invalid", again.reason[:200]) from again
        mismatch = output.model_mismatch or (output.model_actual is not None and output.model_actual != model.model)
        return LlmResult(value, model, output.model_actual, mismatch, runs, artifact)


def as_prompt(role: str, instruction: str, sections: list[str], schema_hint: str) -> str:
    return prompts.task(role, instruction, sections, schema_hint)
