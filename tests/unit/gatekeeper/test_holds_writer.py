"""The single writer of ``contribution_holds`` (open-discovery design §13.3): no other module holds a write
statement for the table, and only the migration creates it."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "ossbot"
VERBS = ("INSERT INTO CONTRIBUTION_HOLDS", "UPDATE CONTRIBUTION_HOLDS", "DELETE FROM CONTRIBUTION_HOLDS",
         "REPLACE INTO CONTRIBUTION_HOLDS")  # fmt: skip


def _sql_strings(path: Path) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(" ".join(node.value.split()).upper())
    return out


def test_only_holds_writes_contribution_holds() -> None:
    offenders = []
    writers = set()
    for path in SRC.rglob("*.py"):
        for sql in _sql_strings(path):
            for verb in VERBS:
                if verb in sql:
                    if path.relative_to(SRC).as_posix() == "gatekeeper/holds.py":
                        writers.add(verb)
                    else:
                        offenders.append(f"{path.relative_to(SRC)}: {verb}")
    assert offenders == []
    assert writers == {"INSERT INTO CONTRIBUTION_HOLDS", "UPDATE CONTRIBUTION_HOLDS"}  # a hold is never deleted


def test_table_created_only_by_the_migration() -> None:
    creators = [p.name for p in SRC.rglob("*.*") if p.suffix in {".py", ".sql"}
                and "CREATE TABLE CONTRIBUTION_HOLDS" in p.read_text(encoding="utf-8").upper()]  # fmt: skip
    assert sorted(creators) == ["0007_open_discovery.sql", "0010_build_env.sql"]  # 0010 rebuilds it (ADR-0025)
