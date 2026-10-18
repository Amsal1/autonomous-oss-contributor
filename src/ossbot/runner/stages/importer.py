"""Operator import, runner side (DESIGN §9.13 steps 3-5): fetch at the fixed base, then ``unbundle``.

FETCHED (source operator) -> the import bundle downloaded from the gatekeeper (sha256 checked
against ``import_bundle_sha256``) -> ``unbundle mode=import`` (``bundle_ref`` = the gatekeeper's
``import_head_ref``) recorded as a succeeded ``unbundle`` stage run -> FETCHED -> IMPLEMENTED (the
gatekeeper's guard checks both). There is no plan: validate runs the per-ecosystem defaults.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ossbot.runner.llm import StageFailed
from ossbot.runner.pipeline import StageContext, StageRun
from ossbot.runner.stages.common import job, write_inputs

STAGE = "import"


def run(sc: StageContext, c: dict[str, Any]) -> None:
    cid = str(c["id"])
    with StageRun(sc, cid, "unbundle", profile="package"):
        data = sc.gk.import_bundle(cid)
        if hashlib.sha256(data).hexdigest() != c.get("import_bundle_sha256"):
            raise StageFailed("import_bundle_sha256")
        inputs = write_inputs(sc, cid, STAGE, {"import.bundle": data})
        args = {"mode": "import", "bundle_ref": str(c["import_head_ref"]), "checkout_sha": str(c["base_sha"]),
                "rev": 1}  # fmt: skip
        job(sc, cid, STAGE, "package", "unbundle", args, "rw", inputs=inputs)
    sc.transition(cid, "FETCHED", "IMPLEMENTED", {"reason": "operator_import"})
