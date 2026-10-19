"""Hermes managed config per capability tier (DESIGN §15.1, §15.5; PLAN items 35/36)."""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from ossbot.common.control_flags import HERMES_TIERS, HermesTier
from ossbot.ctl import hermes_render as hr
from ossbot.gatekeeper.chat import hermes_surface as hs

TEMPLATE = Path(__file__).resolve().parents[3] / "hermes" / "config" / "config.yaml.j2"


def _doc(tier: HermesTier) -> dict:
    return YAML(typ="safe").load(hr.render(tier, TEMPLATE))


@pytest.mark.parametrize("tier", HERMES_TIERS)
def test_surface_per_tier(tier: HermesTier) -> None:
    doc = _doc(tier)
    enabled = doc["platform_toolsets"]["api_server"]
    disabled = doc["agent"]["disabled_toolsets"]
    assert enabled == list(hs.allowed_toolsets(tier))
    assert not set(enabled) & set(disabled)
    assert set(hs.ALWAYS_DISABLED) <= set(disabled)
    assert ("skills" in enabled) is (tier == "self_improve")
    assert ("web" in enabled) is (tier != "locked")
    assert doc["mcp_servers"] == {}
    assert doc["tools"]["tool_search"]["enabled"] == "off"  # a string, not YAML false


@pytest.mark.parametrize("tier", HERMES_TIERS)
def test_gates_and_defence_in_depth(tier: HermesTier) -> None:
    doc = _doc(tier)
    assert doc["approvals"] == {"mode": "manual", "cron_mode": "deny", "unattended_mode": "deny"}
    assert doc["skills"]["write_approval"] is True
    assert doc["skills"]["external_dirs"] == [] and doc["skills"]["create_dir"] == ""
    assert doc["skills"]["project_discovery"] is False and doc["skills"]["inline_shell"] is False
    assert doc["curator"]["enabled"] is False
    assert doc["updates"]["check"] is False and doc["plugins"]["auto_update_check_hours"] == 0
    assert doc["plugins"]["enabled"] == ["ossbot"]
    assert doc["model"]["provider"] == "kiro-acp"
    # images reach the provider as parts (vision role), never Hermes' lossy auxiliary pre-description
    assert doc["model"]["supports_vision"] is True and doc["agent"]["image_input_mode"] == "native"
    assert doc["gateway"]["api_server"]["host"] == "127.0.0.1"


def test_write_is_atomic_and_refuses_links(tmp_path: Path) -> None:
    out = tmp_path / "hermes" / "config.yaml"
    assert hr.write_managed("a: 1\n", out) is True
    assert hr.write_managed("a: 1\n", out) is False
    assert oct(out.stat().st_mode & 0o777) == "0o644"
    out.unlink()
    out.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(RuntimeError):
        hr.write_managed("a: 2\n", out)
    out.unlink()
    (tmp_path / "real").mkdir()
    linked = tmp_path / "linkdir"
    linked.symlink_to(tmp_path / "real")
    with pytest.raises(RuntimeError):
        hr.write_managed("a: 3\n", linked / "config.yaml")


def test_tier_from_flag_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir()
    assert hr.tier_from_flag(control, tmp_path / "no-snapshot") == "locked"  # unreadable default -> locked
    (control / "hermes-tier").write_text('{"tier": "research"}', encoding="utf-8")
    assert hr.tier_from_flag(control, tmp_path / "no-snapshot") == "research"
    (control / "hermes-tier").write_text("garbage", encoding="utf-8")
    assert hr.tier_from_flag(control, tmp_path / "no-snapshot") == "locked"
