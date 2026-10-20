"""``ossctl docs commands [--check]``: the README Telegram command table (DESIGN §18.5, FR-16).

The table is generated from the gatekeeper's command registry (``@command`` decorators in the
built-in modules), so it cannot drift from what the bot answers. ``--check`` exits non-zero when
the README block differs from the registry; CI runs it.
"""

from __future__ import annotations

from pathlib import Path

BEGIN = "<!-- BEGIN ossctl docs commands (generated; edit the registry, then run `ossctl docs commands`) -->"
END = "<!-- END ossctl docs commands -->"


class DocsError(RuntimeError):
    pass


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_table() -> str:
    """Markdown table of every built-in Telegram command, sorted by name."""
    from ossbot.gatekeeper.telegram.commands import load_builtin_modules  # noqa: PLC0415 - lazy, ossctl only
    from ossbot.gatekeeper.telegram.commands.registry import COMMANDS  # noqa: PLC0415

    load_builtin_modules()
    rows = ["| Command | What it does | Safety class |", "|---|---|---|"]
    for spec in sorted(COMMANDS.values(), key=lambda s: s.name):
        rows.append(f"| `/{spec.name}` | {_cell(spec.help)} | {spec.safety} |")
    return "\n".join(rows)


def splice(readme: str, table: str) -> str:
    """``readme`` with the text between the BEGIN and END markers replaced by ``table``."""
    start = readme.find(BEGIN)
    end = readme.find(END)
    if start < 0 or end < 0 or end < start:
        raise DocsError(f"README has no command block (markers {BEGIN!r} ... {END!r})")
    return readme[: start + len(BEGIN)] + "\n" + table + "\n" + readme[end:]


def run(readme_path: Path, *, check: bool) -> bool:
    """Write (or with ``check`` compare) the block; returns True when the file is current."""
    current = readme_path.read_text(encoding="utf-8")
    wanted = splice(current, render_table())
    if wanted == current:
        return True
    if not check:
        readme_path.write_text(wanted, encoding="utf-8", newline="\n")
    return False
