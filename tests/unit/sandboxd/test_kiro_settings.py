"""Baked Kiro settings (DESIGN §10.1 M1, §15.3): the nine cli.json keys (N8 ``toolSearch.enabled``
since FEAT-006 item 30) and the DENY_SYS rules."""

from __future__ import annotations

import json
from pathlib import Path

from ossbot.common.yamlio import safe_load
from ossbot.sandboxd.probes import SETTINGS_REQUIRED, agent_configs_hash, settings_hash

ROOT = Path(__file__).resolve().parents[3]
SETTINGS = ROOT / "sandbox" / "kiro" / "settings"
NINE = {
    "telemetry.enabled": False,
    "app.disableAutoupdates": True,
    "chat.enableKnowledge": False,
    "chat.enableCodeIntelligence": False,
    "chat.enableDelegate": False,
    "chat.enableMainAgentSubagentTool": False,
    "chat.enableWorkflows": False,
    "chat.enableCheckpoint": False,
    "toolSearch.enabled": False,  # §23 item 5 N8: no Tool Search bridge (ADR-0005 addendum, FEAT-006)
}
DENY_SYS = ["/proc/**", "/sys/**", "/dev/**", "/run/**", "/etc/**", "/home/agent/.kiro/**", "/io/output/**"]


def test_cli_json_has_exactly_the_nine_keys() -> None:
    assert json.loads((SETTINGS / "cli.json").read_text(encoding="utf-8")) == NINE


def test_settings_probe_checks_a_subset_of_the_baked_values() -> None:
    assert all(NINE[k] is v for k, v in SETTINGS_REQUIRED.items())


def test_permissions_deny_sys_on_every_file_tool() -> None:
    """V3 policy layout (kiro-cli 2.32, verified live): only a top-level ``rules`` list."""
    doc = safe_load((SETTINGS / "permissions.yaml").read_bytes())
    assert set(doc) == {"rules"}
    rules = doc["rules"]
    for cap in ("fs_read", "fs_write"):
        assert {"capability": cap, "match": DENY_SYS, "effect": "deny"} in rules, cap
    assert {"capability": "shell", "effect": "deny"} in rules
    assert {"capability": "subagent", "effect": "deny"} in rules
    assert not [r for r in rules if r["effect"] != "deny"]  # the global layer only ever denies
    assert {r["capability"] for r in rules} <= {"fs_read", "fs_write", "shell", "mcp", "web_fetch", "web_search",
                                                "subagent"}  # fmt: skip


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


def test_agents_match_the_fixed_trust_tools() -> None:
    """§15.3 table: one agent per Kiro role, tools == TRUST_TOOLS, DENY_SYS on file tools, the coder
    has only the four @ossbot-exec MCP tools (FEAT-006 item 30)."""
    from ossbot.sandboxd.profiles import TRUST_TOOLS

    agents_dir = ROOT / ".kiro" / "agents"
    names = {p.stem for p in agents_dir.glob("ossbot-*.json")}
    assert names == {f"ossbot-{role}" for role in TRUST_TOOLS}
    for role, trusted in TRUST_TOOLS.items():
        doc = json.loads((agents_dir / f"ossbot-{role}.json").read_text(encoding="utf-8"))
        # V3 refuses absolute file:// prompts: the role prompt is inlined in the task (runner/prompts.py)
        assert doc["name"] == f"ossbot-{role}" and f"prompts/{role}.md" in doc["prompt"]
        assert "file://" not in doc["prompt"]
        assert doc["tools"] == doc["allowedTools"] == trusted.split(",")
        assert doc["toolsSettings"]["read"]["deniedPaths"] == DENY_SYS
        assert (ROOT / "prompts" / f"{role}.md").is_file()
        # universal format (V3 engine, kiro-cli 2.32 `agent upgrade`): the same denials as V3 rules,
        # and no catch-all read allow (reads stay limited to the allowed paths)
        rules = doc["permissions"]["rules"]
        assert {"capability": "fs_read", "match": DENY_SYS, "effect": "deny"} in rules
        assert not [r for r in rules if r.get("effect") == "allow" and "match" not in r
                    and r.get("capability") in {"fs_read", "fs_write", "shell"}]  # fmt: skip
        if role == "coder":
            assert set(doc["mcpServers"]) == {"ossbot-exec"}
            assert not {"read", "write", "shell", "glob", "grep", "web_search", "web_fetch"} & set(doc["tools"])
        else:
            assert "mcpServers" not in doc and "shell" not in doc["tools"]


def test_settings_list_parser_reads_the_live_2_32_format() -> None:
    """kiro-cli 2.32 prints ``key = value (global)`` (recorded live by the FEAT-006 probe run)."""
    from ossbot.sandboxd.probes import SETTINGS_REQUIRED, parse_settings

    live = (
        "telemetry.enabled = false (global)\napp.disableAutoupdates = true (global)\n"
        "chat.enableKnowledge = false (global)\nchat.enableCodeIntelligence = false (global)\n"
        "chat.enableDelegate = false (global)\nchat.enableMainAgentSubagentTool = false (global)\n"
        "chat.enableWorkflows = false (global)\nchat.enableCheckpoint = false (global)\n"
        "toolSearch.enabled = false (global)\n"
    )
    values = parse_settings(live)
    assert all(values.get(k) is v for k, v in SETTINGS_REQUIRED.items())
    assert parse_settings("telemetry.enabled = true (global)")["telemetry.enabled"] is True
