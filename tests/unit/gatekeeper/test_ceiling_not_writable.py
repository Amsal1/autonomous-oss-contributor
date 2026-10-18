"""The hard diff ceiling changes only by editing config/policy.yaml plus a human-confirmed reload.

CR-003 U13, CR-004 Amendment B (rows CR4-2h, CR3-U13): no token purpose, Telegram command, confirm
handler or API route can write ``policy.diff``. Checked by enumeration (names) and structurally
(the only gatekeeper code that writes under the config directory is the models.yaml writer).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ossbot.gatekeeper import approvals
from ossbot.gatekeeper.app import create_app
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.telegram.commands import builtin  # noqa: F401  (registers the built-ins)
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


def test_no_gatekeeper_module_names_policy_yaml() -> None:
    """Nothing in the gatekeeper opens policy.yaml by name (the snapshot loader goes through SNAPSHOT_FILES)."""
    for path in GATEKEEPER.rglob("*.py"):
        assert "policy.yaml" not in Path(path).read_text(encoding="utf-8"), path
