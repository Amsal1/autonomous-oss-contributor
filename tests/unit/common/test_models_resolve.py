"""Model resolution (DESIGN §16.1, ADR-0009): indirections, catalog-or-forced, fallbacks, the
mtime-cached reader with its last-good copy, and the list-models parser (§23 item 9).

The resolver tests use a fixed ``TEXT`` (the owner's 2026-10-18 assignment in aligned form) so they
don't depend on the formatting of the live ``config/models.yaml``, which the gatekeeper's writer may
normalize; the repo file itself is checked against the live catalog ids."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ossbot.common.config import ROLE_NAMES, ConfigError, parse_models
from ossbot.common.models import (
    Catalog,
    DistinctUnavailable,
    ModelsFile,
    ModelUnavailable,
    parse_list_models,
    resolve_config,
)

REPO = Path(__file__).resolve().parents[3]
TEXT = """version: 1
default:
  model: claude-opus-5.5
  effort: high
roles:
  chat:       { model: claude-sonnet-5.5, effort: medium }
  planner:    { model: default, effort: high }
  vetter:     { model: claude-sonnet-5.5, effort: medium }
  coder:      { model: default, effort: high }
  reviewer:   { model: claude-sonnet-5.5, effort: xhigh, require_distinct_from: coder }
  summarizer: { model: claude-sonnet-5.5, effort: low }
  scout:      { model: planner, effort: high }
  vision:     { model: claude-sonnet-5.5, effort: medium }
fallbacks: [claude-opus-5.5, claude-sonnet-5.5]
catalog:
  refresh_hours: 24
  notify_new_models: true
"""
CFG = parse_models(TEXT.encode())
ALL = Catalog(frozenset({"claude-opus-5.5", "claude-sonnet-5.5", "claude-opus-5", "claude-haiku-4.5", "glm-5"}))
# The live catalog of 2026-10-18 (kiro-cli 2.32 list_models with the sandbox key, docs/VERIFICATION.md)
LIVE = Catalog(frozenset({
    "auto", "claude-opus-5.5", "claude-sonnet-5.5", "claude-opus-5", "claude-sonnet-5", "claude-opus-4.8",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "claude-opus-4.7", "claude-opus-4.6", "claude-sonnet-4.6",
    "claude-opus-4.5", "claude-sonnet-4.5", "claude-haiku-4.5", "deepseek-3.2", "minimax-m2.5", "minimax-m2.1",
    "glm-5", "qwen3-coder-next",
}))  # fmt: skip


def test_repo_models_yaml_resolves_every_role_without_fallback() -> None:
    """Owner rule: opus-5.5 for deep reasoning (planner, scout, coder), sonnet-5.5 elsewhere."""
    cfg = parse_models((REPO / "config" / "models.yaml").read_bytes())
    want = {"planner": "claude-opus-5.5", "scout": "claude-opus-5.5", "coder": "claude-opus-5.5",
            "reviewer": "claude-sonnet-5.5", "vetter": "claude-sonnet-5.5", "summarizer": "claude-sonnet-5.5",
            "chat": "claude-sonnet-5.5", "vision": "claude-sonnet-5.5"}  # fmt: skip
    assert set(ROLE_NAMES) == set(want)
    for role, model in want.items():
        res = resolve_config(cfg, role, LIVE)
        assert (res.model, res.source != "fallback") == (model, True), role
    assert resolve_config(cfg, "reviewer", LIVE).effort == "xhigh"
    assert list(cfg.fallbacks) == ["claude-opus-5.5", "claude-sonnet-5.5"]


def test_every_role_resolves_with_the_full_catalog() -> None:
    assert set(ROLE_NAMES) >= {"scout", "vision"}
    for role in ROLE_NAMES:
        res = resolve_config(CFG, role, ALL)
        assert ALL.accepts(res.model), role


def test_default_indirection_and_effort() -> None:
    res = resolve_config(CFG, "coder", ALL)
    assert (res.model, res.effort, res.source) == ("claude-opus-5.5", "high", "default")
    chat = resolve_config(CFG, "chat", ALL)
    assert (chat.model, chat.effort, chat.source) == ("claude-sonnet-5.5", "medium", "role")


def test_scout_inherits_planner() -> None:
    res = resolve_config(CFG, "scout", ALL)
    planner = resolve_config(CFG, "planner", ALL)
    assert res.model == planner.model and res.source == "inherited" and res.effort == "high"
    text = TEXT.replace("planner:    { model: default, effort: high }", "planner:    { model: claude-opus-5 }")
    cfg = parse_models(text.encode())
    scout = resolve_config(cfg, "scout", ALL)
    assert scout.model == "claude-opus-5" and scout.effort == "high"  # own effort wins
    assert resolve_config(cfg, "planner", ALL).effort == "high"  # inherits default.effort


def test_effort_inherits_from_default_when_unset() -> None:
    text = TEXT.replace("coder:      { model: default, effort: high }", "coder:      { model: default }")
    text = text.replace("model: claude-opus-5.5\n  effort: high", "model: claude-opus-5.5\n  effort: xhigh", 1)
    assert "effort: xhigh" in text
    assert resolve_config(parse_models(text.encode()), "coder", ALL).effort == "xhigh"


def test_non_catalog_id_falls_back_in_order() -> None:
    cat = Catalog(frozenset({"claude-sonnet-5.5", "claude-haiku-4.5"}))
    res = resolve_config(CFG, "coder", cat)  # opus-5.5 missing: its own id as fallback is skipped
    assert (res.model, res.source, res.requested) == ("claude-sonnet-5.5", "fallback", "claude-opus-5.5")
    vetter = resolve_config(CFG, "vetter", Catalog(frozenset({"claude-opus-5.5"})))
    assert (vetter.model, vetter.source) == ("claude-opus-5.5", "fallback")


def test_forced_id_is_accepted() -> None:
    cat = Catalog(frozenset(), forced=frozenset({"claude-opus-5.5"}))
    assert resolve_config(CFG, "planner", cat).model == "claude-opus-5.5"
    assert Catalog.from_rows([{"model_id": "x", "forced": True}, {"model_id": "y", "forced": False}]) == Catalog(
        frozenset({"y"}), frozenset({"x"})
    )


def test_unresolvable_is_model_unavailable() -> None:
    with pytest.raises(ModelUnavailable) as err:
        resolve_config(CFG, "coder", Catalog(frozenset({"other"})))
    assert err.value.error_class == "model_unavailable"
    with pytest.raises(ModelUnavailable):
        resolve_config(CFG, "janitor", ALL)


def test_reviewer_distinct_from_coder_after_fallback() -> None:
    """require_distinct_from stays enforced after fallback; a collision raises, never runs."""
    text = TEXT.replace("claude-sonnet-5.5, effort: xhigh, require", "default, effort: xhigh, require")
    cfg = parse_models(text.encode())
    res = resolve_config(cfg, "reviewer", ALL)
    assert res.model == "claude-sonnet-5.5" and res.source == "fallback"  # opus-5.5 = coder's, skipped
    only = Catalog(frozenset({"claude-opus-5.5"}))
    with pytest.raises(DistinctUnavailable):
        resolve_config(cfg, "reviewer", only)  # nothing distinct: the runner pauses instead
    sonnet_gone = Catalog(frozenset({"claude-opus-5.5"}))  # the real config, sonnet-5.5 vanished
    with pytest.raises(DistinctUnavailable):
        resolve_config(CFG, "reviewer", sonnet_gone)
    assert resolve_config(CFG, "coder", sonnet_gone).model == "claude-opus-5.5"


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("planner:    { model: default, effort: high }", "planner:    { model: scout }", "cycle"),
        ("chat:       { model: claude-sonnet-5.5, effort: medium }", "chat:       { model: chat }", "cycle"),
        ("model: claude-opus-5.5", "model: coder", "default.model"),
        ("model: claude-opus-5.5", "model: default", "default.model"),
        ("[claude-opus-5.5, claude-sonnet-5.5]", "[planner]", "fallbacks"),
        ("require_distinct_from: coder", "require_distinct_from: reviewer", "itself"),
    ],
)
def test_bad_references_rejected(old: str, new: str, match: str) -> None:
    assert old in TEXT
    with pytest.raises(ConfigError, match=match):
        parse_models(TEXT.replace(old, new, 1).encode())


def test_scout_and_vision_required() -> None:
    text = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("vision:"))
    with pytest.raises(ConfigError, match="vision"):
        parse_models(text.encode())


# ------------------------------------------------------------------- ModelsFile
def _bump(path: Path, text: str) -> None:
    st = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_models_file_caches_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(TEXT, encoding="utf-8")
    reader = ModelsFile(path)
    first = reader.get()
    assert reader.get() is first  # mtime cache
    _bump(path, TEXT.replace("claude-sonnet-5.5, effort: low", "claude-opus-5, effort: low"))
    assert reader.resolve("summarizer", ALL).model == "claude-opus-5"  # read fresh after a change


def test_broken_file_keeps_last_good_and_alerts_once(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    good = tmp_path / "models.last-good.yaml"
    path.write_text(TEXT, encoding="utf-8")
    alerts: list[str] = []
    reader = ModelsFile(path, last_good_path=good, on_broken=alerts.append)
    assert reader.resolve("coder", ALL).model == "claude-opus-5.5"
    assert good.read_text(encoding="utf-8") == TEXT
    _bump(path, TEXT.replace("version: 1", "version: 9"))
    assert reader.resolve("coder", ALL).model == "claude-opus-5.5"
    assert reader.get().default.model == "claude-opus-5.5"
    assert len(alerts) == 1 and "last good" in alerts[0]
    # a restart with the broken file still resolves through the persisted copy
    fresh = ModelsFile(path, last_good_path=good, on_broken=alerts.append)
    assert fresh.resolve("coder", ALL).model == "claude-opus-5.5"
    assert good.read_text(encoding="utf-8") == TEXT  # the broken version never overwrites it


def test_broken_without_last_good_is_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("version: [", encoding="utf-8")
    with pytest.raises(ModelUnavailable):
        ModelsFile(path).get()


# -------------------------------------------------------------- list-models output
def test_parse_model_list_result_shape() -> None:
    raw = json.dumps(
        {
            "models": [
                {"model_id": "claude-opus-5.5", "model_name": "Claude Opus 5.5", "description": "x",
                 "supported_input_types": ["TEXT", "IMAGE"], "rate_multiplier": 2.2},
                {"model_id": "claude-haiku-4.5", "model_name": "claude-haiku-4.5"},
                {"model_id": "bad id"},
                {"model_id": "claude-opus-5.5"},
                "junk",
            ],
            "default_model": "claude-opus-5.5",
        }
    ).encode()  # fmt: skip
    entries = parse_list_models(raw)
    assert [e.model_id for e in entries] == ["claude-opus-5.5", "claude-haiku-4.5"]
    assert entries[0].name == "Claude Opus 5.5" and entries[0].input_types == ["TEXT", "IMAGE"]
    assert entries[1].input_types is None


def test_parse_bare_list_and_acp_shape() -> None:
    assert [e.model_id for e in parse_list_models(b'[{"modelId": "auto"}, {"id": "m2", "name": "M2"}]')] == [
        "auto",
        "m2",
    ]
    acp = {"currentModelId": "auto", "availableModels": [{"modelId": "claude-sonnet-4.6", "name": "s"}]}
    assert parse_list_models(json.dumps(acp).encode())[0].model_id == "claude-sonnet-4.6"


def test_parse_caps_at_200_and_rejects_non_json() -> None:
    raw = json.dumps({"models": [{"model_id": f"m{i}"} for i in range(250)]}).encode()
    assert len(parse_list_models(raw)) == 200
    with pytest.raises(ValueError, match="not JSON"):
        parse_list_models(b"\xff")
    with pytest.raises(ValueError, match="no model list"):
        parse_list_models(b'{"x": 1}')


def test_parse_drops_bad_input_types_but_keeps_entry() -> None:
    raw = json.dumps({"models": [{"model_id": "m", "supported_input_types": ["bad type!"]}]}).encode()
    entries = parse_list_models(raw)
    assert entries[0].model_id == "m" and entries[0].input_types is None
