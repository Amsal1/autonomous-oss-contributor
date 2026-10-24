"""One Kiro role invocation through sandboxd (DESIGN §15.3, §16.1): the ``kiro`` pipeline backend.

Model and effort are resolved fresh from ``config/models.yaml`` against the gatekeeper's catalog
(catalog-or-forced, fallbacks; ``model_unavailable`` sends nothing to sandboxd). The task prompt and
its inputs are written into the job's input dir under ``/srv/ossbot/runner/inputs``. sandboxd runs
``kiro-cli chat`` headless in the ``kiro`` image entrypoint behind the probe gate. Output is the last
fenced ```json block, validated; an invalid answer gets exactly one repair retry, a second failure
fails the stage. Moved unchanged from ``runner.llm`` (ADR-0022).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ossbot.common.backends import KIRO_HEADLESS, BackendSpec
from ossbot.common.models import Catalog, DistinctUnavailable, ModelsFile, ModelUnavailable, ResolvedModel
from ossbot.runner import output_parse
from ossbot.runner.backends.base import (
    ROLE_PROFILE,
    Check,
    ExecNetwork,
    JobEgress,
    LlmResult,
    exec_network_of,
    new_run_id,
)
from ossbot.runner.errors import Interrupted, OutputInvalidFailed, StageFailed, sandbox_run
from ossbot.runner.gk_client import Gatekeeper, GkError
from ossbot.runner.sbx_client import Sandbox


class KiroHeadlessBackend:
    spec: BackendSpec = KIRO_HEADLESS

    def __init__(
        self,
        gk: Gatekeeper,
        sbx: Sandbox,
        models: ModelsFile,
        inputs_root: Path,
        tick: Callable[[], None] | None = None,
        on_job: Callable[[str], None] | None = None,
    ) -> None:
        self.gk = gk
        self.sbx = sbx
        self.models = models
        self.inputs_root = inputs_root
        self.tick = tick
        self.on_job = on_job  # cid -> None, before each job (the sandbox-resource sweep note)

    def resolve(self, role: str) -> ResolvedModel:
        try:
            return self.models.resolve(role, Catalog.from_rows(self.gk.catalog()))
        except DistinctUnavailable as exc:
            # never a review by the coder's own model: pause (owner alert) and re-queue the stage
            with contextlib.suppress(GkError):
                self.gk.pause(f"model_collision:{role}")
            raise Interrupted(f"model_collision: {exc}") from exc
        except ModelUnavailable as exc:
            raise StageFailed("model_unavailable", str(exc)) from exc

    def list_models(self) -> list[dict[str, Any]]:
        """The gatekeeper's catalog (filled by the ``list_models`` job), the ids ``resolve`` accepts."""
        return self.gk.catalog()

    def probe_surface(self) -> BackendSpec:
        return self.spec

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
              timeout_s: int, egress: JobEgress | None = None,
              ) -> tuple[output_parse.KiroOutput, str, Path | None, ExecNetwork]:  # fmt: skip
        run_id = new_run_id(role)
        prompt_file = f"{role}-task.md"
        inputs = self._write_inputs(cid, run_id, {**files, prompt_file: text})
        profile, repo_mode = ROLE_PROFILE[role]
        spec: dict[str, Any] = {
            "run_id": run_id, "cid": cid, "profile": profile, "entrypoint": "kiro", "repo_mode": repo_mode,
            "args": {"role": role, "prompt_file": prompt_file, "model": model.model, "effort": model.effort},
            "inputs": inputs, "timeout_s": timeout_s,
        }  # fmt: skip
        if egress is not None:  # ADR-0024: a coder job states its egress (sandboxd refuses it otherwise)
            spec["egress"] = egress
        if self.on_job is not None:
            self.on_job(cid)
        job = sandbox_run(self.sbx, spec, self.tick)
        if job.get("state") != "succeeded":
            raise StageFailed(str(job.get("error_class") or "kiro_failed"), f"{role} job {run_id}")
        artifact = Path(str(job["artifact_dir"])) if job.get("artifact_dir") else None
        events = (artifact / f"{role}.events.jsonl").read_bytes() if artifact else b""
        stderr_path = artifact / f"{role}.stderr" if artifact else None
        stderr = stderr_path.read_bytes() if stderr_path is not None and stderr_path.is_file() else b""
        output = output_parse.read_events(events, stderr)
        if output.agent_fallback:  # the role ran without its agent (tools, paths): never use its answer
            raise StageFailed("agent_config_rejected", f"{role} job {run_id}")
        return output, run_id, artifact, exec_network_of(job)

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
        egress: JobEgress | None = None,
    ) -> LlmResult[T]:
        """``check`` adds a deterministic rule on top of the schema; a violation gets the same
        single repair retry as an invalid answer. ``egress`` goes into the job spec when given (the coder)."""
        model = self.resolve(role)
        files = files or {}

        def parse(answer: str) -> T:
            value = output_parse.parse_model(answer, out_model)
            reason = check(value) if check is not None else None
            if reason is not None:
                raise output_parse.OutputInvalid(reason)
            return value

        output, run_id, artifact, net = self._once(cid, role, model, text, files, timeout_s, egress)
        runs = [run_id]
        try:
            value = parse(output.text)
        except output_parse.OutputInvalid as exc:  # one repair retry, then the stage fails
            repair = text + "\n\n" + output_parse.repair_prompt(exc.reason, schema_hint)
            output, run_id, artifact, net = self._once(cid, role, model, repair, files, timeout_s, egress)
            runs.append(run_id)
            try:
                value = parse(output.text)
            except output_parse.OutputInvalid as again:
                raise OutputInvalidFailed(again.reason[:200], output.text) from again
        mismatch = output.model_mismatch or (output.model_actual is not None and output.model_actual != model.model)
        # build-env §8.1: the network of the run whose answer is used (the coder's agent-exec; "none" otherwise)
        return LlmResult(value, model, output.model_actual, mismatch, runs, artifact, net)

    run_role = invoke  # the ModelBackend name; the stages keep calling ``invoke``
