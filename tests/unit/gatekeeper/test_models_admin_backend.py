"""The models.yaml writer keeps the ``backend`` key (ADR-0022): ruamel round-trip, value and position."""

from __future__ import annotations

from ossbot.common.config import parse_models
from ossbot.common.schemas import CatalogEntry
from ossbot.gatekeeper.context import AppContext

ACTOR = "uid:1000/operator"


def test_set_model_keeps_an_explicit_backend(ctx: AppContext) -> None:
    path = ctx.models.path
    text = path.read_text(encoding="utf-8").replace("\nversion: 1\n", "\nversion: 1\nbackend: kiro\n", 1)
    path.write_text(text, encoding="utf-8")
    ctx.models.upsert_catalog([CatalogEntry(model_id="claude-next")], "uid:2001/worker")
    diff = ctx.models.set_model("coder", "claude-next", None, False, ACTOR)
    after = path.read_text(encoding="utf-8")
    assert "backend" not in diff and "\nversion: 1\nbackend: kiro\n" in after
    cfg = parse_models(after.encode("utf-8"))
    assert cfg.backend == "kiro" and cfg.roles.coder.model == "claude-next"


def test_set_model_without_the_key_stays_without_it(ctx: AppContext) -> None:
    ctx.models.upsert_catalog([CatalogEntry(model_id="claude-next")], "uid:2001/worker")
    ctx.models.set_model("coder", "claude-next", None, False, ACTOR)
    after = ctx.models.path.read_text(encoding="utf-8")
    assert "\nbackend:" not in after and parse_models(after.encode("utf-8")).backend == "kiro"
