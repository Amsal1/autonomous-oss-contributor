"""The hard diff ceiling changes only by editing config/policy.yaml plus a human-confirmed reload.

CR-003 U13, CR-004 Amendment B (rows CR4-2h, CR3-U13): no token purpose, Telegram command, confirm
handler or API route can write ``policy.diff``. Checked by enumeration (names) and structurally
(the only gatekeeper code that writes under the config directory is the models.yaml writer).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from ossbot.common.config import OpenDiscoveryPolicy
from ossbot.common.schemas import PrefsUpdate
from ossbot.gatekeeper import approvals, discovery_prefs
from ossbot.gatekeeper.app import create_app
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram import actions
from ossbot.gatekeeper.telegram.commands import load_builtin_modules
from ossbot.gatekeeper.telegram.commands.registry import COMMANDS, CONFIRMS
from tests.conftest import REPO_ROOT
from tests.unit.gatekeeper.test_authz import api_routes

GATEKEEPER = REPO_ROOT / "src" / "ossbot" / "gatekeeper"
MIGRATIONS = GATEKEEPER / "migrations"
# A name that would mean "change the ceiling". `diff_page` is a read-only view purpose.
CEILING_RE = re.compile(r"ceiling|max_files|max_changed|max_test|policy|diff(?!_page\b)")


def token_purposes() -> set[str]:
    """Every purpose listed in the ui_tokens comments of the migrations, plus the code's tuples."""
    out: set[str] = set()
    for sql in sorted(MIGRATIONS.glob("*.sql")):
        for line in sql.read_text(encoding="utf-8").splitlines():
            if re.match(r"\s*purpose TEXT", line) and "--" in line:
                out.update(p.strip() for p in line.split("--", 1)[1].split("|") if p.strip())
    for name in dir(approvals):
        value = getattr(approvals, name)
        if name.endswith("PURPOSES") and isinstance(value, tuple | frozenset | set):
            out.update(str(v) for v in value)
    return out


load_builtin_modules()  # every built-in command, confirm handler and kit action registers on import
# read-only view purposes whose names contain "diff"
VIEW_ONLY = frozenset({"interdiff"})


def test_no_kit_action_writes_the_ceiling() -> None:
    """The kit actions of the proposal, rescope/ceiling, /size and /filters surfaces (PLAN item 46)."""
    assert {"proposal_skip", "rescope_recheck", "size_set", "filters_set"} <= set(actions.ACTIONS)
    assert [p for p in actions.ACTIONS if CEILING_RE.search(p) and p not in VIEW_ONLY] == []


def test_purposes_are_enumerated() -> None:
    purposes = token_purposes()
    assert {"approve", "confirm", "reload_confirm", "diff_page"} <= purposes


def test_no_token_purpose_writes_the_ceiling() -> None:
    assert [p for p in token_purposes() if CEILING_RE.search(p)] == []


def test_no_command_or_confirm_handler_writes_the_ceiling() -> None:
    assert COMMANDS, "built-in commands not registered"
    names = [*COMMANDS, *CONFIRMS]
    assert [n for n in names if CEILING_RE.search(n)] == []
    for spec in COMMANDS.values():
        assert not CEILING_RE.search(spec.help.lower().replace("diff page", "")), spec.name


def test_no_api_route_writes_the_ceiling(ctx: AppContext) -> None:
    routes = api_routes(create_app(ctx, housekeeping=False).routes)
    assert routes
    writes = [r for r in routes if r.methods & {"POST", "PUT", "PATCH", "DELETE"}]
    assert [r.path for r in writes if CEILING_RE.search(r.path)] == []


OPEN_COUNT_RE = re.compile(r"min_stars|search_budget|repos_per_cycle|topics_per_cycle|reserved_slots|cooldown_days"
                           r"|pushed_within|language_weight|issue_labels")  # fmt: skip


def test_open_discovery_counts_are_config_only(ctx: AppContext) -> None:
    """Post-v1 open discovery (design §3.2): only enabled, topics and languages_preferred are tunable;
    every other discovery.open key is refused by discovery_prefs (config_only), and no route, kit action,
    command or token purpose is named after one."""
    tunable = {k.removeprefix("open_") for k in discovery_prefs.OPEN_PREF_KEYS}
    assert tunable == {"enabled", "topics", "languages_preferred"}
    config_only = {k.removeprefix("open_") for k in discovery_prefs.CONFIG_ONLY_OPEN_KEYS}
    assert config_only | tunable == set(OpenDiscoveryPolicy.model_fields) and not config_only & tunable
    for key in discovery_prefs.CONFIG_ONLY_OPEN_KEYS:
        with pytest.raises(discovery_prefs.PrefsRefused, match="config_only"):
            ctx.discovery_prefs.update(PrefsUpdate(filters={key: 1}), "tg:4242")
    names = [*actions.ACTIONS, *COMMANDS, *CONFIRMS, *token_purposes()]
    names += [r.path for r in api_routes(create_app(ctx, housekeeping=False).routes)]
    assert [n for n in names if OPEN_COUNT_RE.search(n)] == []


def _config_dir_users() -> dict[str, list[str]]:
    """module -> source lines that mention ``config_dir`` (the only handle on config/ files)."""
    out: dict[str, list[str]] = {}
    for path in sorted(GATEKEEPER.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "config_dir"
        ]
        if hits:
            out[path.relative_to(GATEKEEPER).as_posix()] = hits
    return out


def test_only_models_yaml_is_written_under_config() -> None:
    """config_snapshot reads the four snapshot files; context hands ModelsAdmin models.yaml only."""
    users = _config_dir_users()
    assert set(users) == {"config_snapshot.py", "context.py"}
    context_src = (GATEKEEPER / "context.py").read_text(encoding="utf-8")
    assert re.findall(r"config_dir / \"([^\"]+)\"", context_src) == ["models.yaml"]
    snapshot_src = (GATEKEEPER / "config_snapshot.py").read_text(encoding="utf-8")
    for writer in ("write_text", "write_bytes", "os.replace", "open("):
        for line in snapshot_src.splitlines():
            if writer in line:
                assert "config_dir" not in line, line


def test_no_gatekeeper_module_opens_policy_yaml() -> None:
    """Nothing in the gatekeeper builds a path to policy.yaml (the snapshot loader goes through
    SNAPSHOT_FILES); the name may appear only as an error-report key."""
    for path in GATEKEEPER.rglob("*.py"):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if "policy.yaml" in line:
                assert not re.search(r"open\(|write|config_dir|/ *\"policy\.yaml|Path\(", line), (path, line)
