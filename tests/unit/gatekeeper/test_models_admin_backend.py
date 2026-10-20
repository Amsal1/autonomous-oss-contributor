"""The models.yaml writer keeps the ``backend`` key (ADR-0022): ruamel round-trip, value and position."""

from __future__ import annotations

import re

from ossbot.common.config import parse_models
from ossbot.common.schemas import CatalogEntry
from ossbot.gatekeeper.context import AppContext

ACTOR = "uid:1000/operator"


def test_set_model_keeps_an_explicit_backend(ctx: AppContext) -> None:
    """The fixture copies the shipped config/models.yaml, which sets `backend: kiro` after `version`."""
    path = ctx.models.path
    before = path.read_text(encoding="utf-8")
    line = re.search(r"(?m)^backend: kiro\b[^\n]*$", before)
    assert line and f"\nversion: 1\n{line.group(0)}\n" in before
    ctx.models.upsert_catalog([CatalogEntry(model_id="claude-next")], "uid:2001/worker")
    diff = ctx.models.set_model("coder", "claude-next", None, False, ACTOR)
    after = path.read_text(encoding="utf-8")
    assert "backend" not in diff and f"\nversion: 1\n{line.group(0)}\n" in after
    cfg = parse_models(after.encode("utf-8"))
    assert cfg.backend == "kiro" and cfg.roles.coder.model == "claude-next"


def test_set_model_without_the_key_stays_without_it(ctx: AppContext) -> None:
    path = ctx.models.path
    path.write_text(re.sub(r"(?m)^backend:[^\n]*\n", "", path.read_text(encoding="utf-8")), encoding="utf-8")
    ctx.models.upsert_catalog([CatalogEntry(model_id="claude-next")], "uid:2001/worker")
    ctx.models.set_model("coder", "claude-next", None, False, ACTOR)
    after = path.read_text(encoding="utf-8")
    assert "\nbackend:" not in after and parse_models(after.encode("utf-8")).backend == "kiro"
