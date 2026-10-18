"""Catalog job (DESIGN §16.3, M11): ``list_models`` in the ``catalog`` profile -> ``POST /v1/models/catalog``.

Runs at runner start when the catalog is empty or older than ``catalog.refresh_hours``, daily, and
on ``ossctl models refresh`` (a gatekeeper-held request the scheduler claims). No prompt, no
inputs, ``kiro_core`` egress only, exempt from the probe gate. The gatekeeper sends the new-model
FYIs (one summary on the first fill).
"""

from __future__ import annotations

from ossbot.common.models import parse_list_models
from ossbot.runner.llm import StageFailed, new_run_id
from ossbot.runner.pipeline import StageContext
from ossbot.runner.stages.common import artifact

STAGE = "catalog"
CATALOG_CID = "sys-catalog"


def run(sc: StageContext) -> list[str]:
    sc.gate.check(STAGE)
    spec = {"run_id": new_run_id(STAGE), "cid": CATALOG_CID, "profile": "catalog", "entrypoint": "list_models",
            "args": {}, "repo_mode": "none", "inputs": {}, "timeout_s": 120}  # fmt: skip
    done = sc.sbx.run(spec, tick=lambda: sc.gate.check(STAGE))
    if done.get("state") != "succeeded":
        raise StageFailed(str(done.get("error_class") or "list_models_failed"))
    entries = parse_list_models(artifact(done, "models.json").read_bytes())
    if not entries:
        raise StageFailed("catalog_empty")
    payload = [e.model_dump(exclude_none=True) for e in entries]
    return sc.gk.upsert_catalog(payload)
