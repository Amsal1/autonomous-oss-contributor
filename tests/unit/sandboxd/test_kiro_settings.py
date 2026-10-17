"""Baked Kiro settings (DESIGN §10.1 M1, §15.3): the eight cli.json keys and the DENY_SYS rules."""

from __future__ import annotations

import json
from pathlib import Path

from ossbot.common.yamlio import safe_load
from ossbot.sandboxd.probes import SETTINGS_REQUIRED, agent_configs_hash, settings_hash

ROOT = Path(__file__).resolve().parents[3]
SETTINGS = ROOT / "sandbox" / "kiro" / "settings"
EIGHT = {
    "telemetry.enabled": False,
    "app.disableAutoupdates": True,
    "chat.enableKnowledge": False,
    "chat.enableCodeIntelligence": False,
    "chat.enableDelegate": False,
    "chat.enableMainAgentSubagentTool": False,
    "chat.enableWorkflows": False,
    "chat.enableCheckpoint": False,
}
DENY_SYS = ["/proc/**", "/sys/**", "/dev/**", "/run/**", "/etc/**", "/home/agent/.kiro/**", "/io/output/**"]


def test_cli_json_has_exactly_the_eight_keys() -> None:
    assert json.loads((SETTINGS / "cli.json").read_text(encoding="utf-8")) == EIGHT


def test_settings_probe_checks_a_subset_of_the_baked_values() -> None:
    assert all(EIGHT[k] is v for k, v in SETTINGS_REQUIRED.items())


def test_permissions_deny_sys_on_every_file_tool() -> None:
    doc = safe_load((SETTINGS / "permissions.yaml").read_bytes())
    for tool in ("read", "write", "glob", "grep"):
        assert doc["tools"][tool]["deniedPaths"] == DENY_SYS, tool
    assert set(doc["agents"]["ossbot-coder"]["deniedTools"]) == {"read", "write", "shell", "glob", "grep"}


def test_probe_hashes_change_with_content() -> None:
    cli = (SETTINGS / "cli.json").read_bytes()
    perms = (SETTINGS / "permissions.yaml").read_bytes()
    assert settings_hash(cli) == settings_hash(cli)
    assert settings_hash(cli) != settings_hash(cli + b" ")
    agents = {"ossbot-coder.json": b"{}"}
    assert agent_configs_hash(agents, perms) != agent_configs_hash({"ossbot-coder.json": b"{ }"}, perms)
    assert agent_configs_hash(agents, perms) != agent_configs_hash(agents, perms + b"#")


def test_llm_image_bakes_these_files_and_kiro_only_there() -> None:
    llm = (ROOT / "sandbox" / "images" / "llm" / "Dockerfile").read_text(encoding="utf-8")
    full = (ROOT / "sandbox" / "images" / "full" / "Dockerfile").read_text(encoding="utf-8")
    assert "sandbox/kiro/settings/cli.json sandbox/kiro/settings/permissions.yaml" in llm
    # no kiro-cli, Kiro settings or kiro entrypoints in the code-running image
    for marker in ("KIRO_CLI", "/opt/kiro-cli", "kiro.sh", "list_models.sh", "kiro/settings", "exec_mcp"):
        assert marker not in full, marker
    assert "KIRO_CLI_SHA256" in llm
