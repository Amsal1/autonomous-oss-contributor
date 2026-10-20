"""``ossctl docs commands``: the README table is generated from the registry and ``--check`` fails on drift."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ossbot.ctl import cli
from ossbot.ctl import docs_commands as dc
from ossbot.gatekeeper.telegram.commands.registry import COMMANDS

ROOT = Path(__file__).resolve().parents[3]


def _readme(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "README.md"
    path.write_text(f"# x\n\n{dc.BEGIN}\n{body}\n{dc.END}\n\ntail\n", encoding="utf-8")
    return path


def test_table_lists_every_registered_command() -> None:
    table = dc.render_table()
    assert COMMANDS, "the built-in modules registered no command"
    for name, spec in COMMANDS.items():
        assert f"| `/{name}` |" in table
        assert f"| {spec.safety} |" in table
    assert table.splitlines()[0] == "| Command | What it does | Safety class |"


def test_cells_escape_pipes() -> None:
    assert dc._cell("a | b\nc") == "a \\| b c"


def test_check_fails_on_drift_and_write_fixes_it(tmp_path: Path) -> None:
    readme = _readme(tmp_path, "| stale |")
    runner = CliRunner()
    res = runner.invoke(cli.app, ["docs", "commands", "--check", "--readme", str(readme)])
    assert res.exit_code == 1
    assert "out of date" in res.output
    assert "| stale |" in readme.read_text(encoding="utf-8")  # --check never writes

    res = runner.invoke(cli.app, ["docs", "commands", "--readme", str(readme)])
    assert res.exit_code == 0
    text = readme.read_text(encoding="utf-8")
    assert "| stale |" not in text and text.endswith(f"{dc.END}\n\ntail\n")

    res = runner.invoke(cli.app, ["docs", "commands", "--check", "--readme", str(readme)])
    assert res.exit_code == 0, res.output


def test_missing_markers_is_an_error(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# no block\n", encoding="utf-8")
    with pytest.raises(dc.DocsError):
        dc.run(readme, check=True)
    res = CliRunner().invoke(cli.app, ["docs", "commands", "--check", "--readme", str(readme)])
    assert res.exit_code == 1


def test_repo_readme_is_current() -> None:
    assert dc.run(ROOT / "README.md", check=True), "run: ossctl docs commands"
