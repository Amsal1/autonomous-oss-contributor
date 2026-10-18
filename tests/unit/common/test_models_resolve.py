"""Model resolution (DESIGN §16.1, ADR-0009): indirections, catalog-or-forced, fallbacks, the
mtime-cached reader with its last-good copy, and the list-models parser (§23 item 9)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ossbot.common.config import ROLE_NAMES, ConfigError, parse_models
from ossbot.common.models import (
    Catalog,
    ModelsFile,
    ModelUnavailable,
    parse_list_models,
    resolve_config,
)

REPO = Path(__file__).resolve().parents[3]
TEXT = (REPO / "config" / "models.yaml").read_text(encoding="utf-8")
CFG = parse_models(TEXT.encode())
ALL = Catalog(
    frozenset({"claude-opus-5.5", "claude-sonnet-5", "claude-sonnet-5.5", "claude-haiku-4.5", "claude-opus-5"})
)


def test_every_role_resolves_with_the_full_catalog() -> None:
    assert set(ROLE_NAMES) >= {"scout", "vision"}
    for role in ROLE_NAMES:
        res = resolve_config(CFG, role, ALL)
        assert ALL.accepts(res.model), role


def test_default_indirection_and_effort() -> None:
    res = resolve_config(CFG, "coder", ALL)
    assert (res.model, res.effort, res.source) == ("claude-opus-5.5", "high", "default")
    chat = resolve_config(CFG, "chat", ALL)
    assert (chat.model, chat.effort) == ("claude-opus-5.5", "medium")


def test_scout_inherits_planner() -> None:
    res = resolve_config(CFG, "scout", ALL)
    planner = resolve_config(CFG, "planner", ALL)
    assert res.model == planner.model and res.source == "inherited" and res.effort == "high"
    text = TEXT.replace("planner:    { model: default, effort: high }", "planner:    { model: claude-sonnet-5 }")
    cfg = parse_models(text.encode())
    scout = resolve_config(cfg, "scout", ALL)
    assert scout.model == "claude-sonnet-5" and scout.effort == "high"  # own effort wins
    assert resolve_config(cfg, "planner", ALL).effort == "high"  # inherits default.effort


def test_effort_inherits_from_default_when_unset() -> None:
    text = TEXT.replace("coder:      { model: default, effort: high }", "coder:      { model: default }")
    text = text.replace("model: claude-opus-5.5\n  effort: high", "model: claude-opus-5.5\n  effort: xhigh", 1)
    assert "effort: xhigh" in text
    assert resolve_config(parse_models(text.encode()), "coder", ALL).effort == "xhigh"


def test_non_catalog_id_falls_back_in_order() -> None:
    cat = Catalog(frozenset({"claude-sonnet-5", "claude-haiku-4.5"}))
    res = resolve_config(CFG, "coder", cat)
    assert (res.model, res.source, res.requested) == ("claude-sonnet-5", "fallback", "claude-opus-5.5")
    cat2 = Catalog(frozenset({"claude-opus-5", "claude-sonnet-5"}))
    assert resolve_config(CFG, "coder", cat2).model == "claude-opus-5"


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


def test_reviewer_distinct_from_coder() -> None:
    text = TEXT.replace("claude-sonnet-5.5, effort: high, require", "default, effort: high, require")
    cfg = parse_models(text.encode())
    res = resolve_config(cfg, "reviewer", ALL)
    assert res.model == "claude-opus-5" and res.source == "fallback"  # first fallback != coder's model
    only = Catalog(frozenset({"claude-opus-5.5"}))
    assert resolve_config(cfg, "reviewer", only).model == "claude-opus-5.5"  # nothing distinct: kept, runner flags


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("planner:    { model: default, effort: high }", "planner:    { model: scout }", "cycle"),
        ("chat:       { model: default, effort: medium }", "chat:       { model: chat }", "cycle"),
        ("model: claude-opus-5.5", "model: coder", "default.model"),
        ("model: claude-opus-5.5", "model: default", "default.model"),
        ("[claude-opus-5, claude-sonnet-5]", "[planner]", "fallbacks"),
        ("require_distinct_from: coder", "require_distinct_from: reviewer", "itself"),
    ],
)
def test_bad_references_rejected(old: str, new: str, match: str) -> None:
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
    _bump(path, TEXT.replace("claude-haiku-4.5, effort: low", "claude-sonnet-5, effort: low"))
    assert reader.resolve("summarizer", ALL).model == "claude-sonnet-5"  # read fresh after a change


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
