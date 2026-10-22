"""The single writer of ``discovered_targets`` (design §8.2): no other module holds a write statement,
the table is created only by the migration, and the writer refuses columns outside its closed sets."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ossbot.gatekeeper import discovered_targets_writer as writer
from ossbot.gatekeeper.context import AppContext

SRC = Path(__file__).resolve().parents[3] / "src" / "ossbot"
VERBS = ("INSERT INTO DISCOVERED_TARGETS", "UPDATE DISCOVERED_TARGETS", "DELETE FROM DISCOVERED_TARGETS")


def _sql_strings(path: Path) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(" ".join(node.value.split()).upper())
    return out


def test_only_the_writer_writes_discovered_targets() -> None:
    offenders = []
    writers = set()
    for path in SRC.rglob("*.py"):
        for sql in _sql_strings(path):
            for verb in VERBS:
                if verb in sql:
                    if path.name == "discovered_targets_writer.py":
                        writers.add(verb)
                    else:
                        offenders.append(f"{path.relative_to(SRC)}: {verb}")
    assert offenders == []
    assert writers == {"INSERT INTO DISCOVERED_TARGETS", "UPDATE DISCOVERED_TARGETS"}  # it never deletes


def test_table_created_only_by_the_migration() -> None:
    creators = [p.name for p in SRC.rglob("*.*") if p.suffix in {".py", ".sql"}
                and "CREATE TABLE DISCOVERED_TARGETS" in p.read_text(encoding="utf-8").upper()]  # fmt: skip
    assert creators == ["0007_open_discovery.sql"]


def test_writer_refuses_unknown_columns(ctx: AppContext) -> None:
    with pytest.raises(writer.WriterError):
        writer.insert_row(ctx.conn, {"repo": "a/b", "source": "search", "status": "candidate", "x; DROP": 1})
    with pytest.raises(writer.WriterError):
        writer.update_facts(ctx.conn, "a/b", {"status": "kept"}, "t")
    with pytest.raises(writer.WriterError):
        writer.set_status(ctx.conn, "a/b", "kept", "t", source="owner")
    with pytest.raises(writer.WriterError, match="no discovered_targets row"):
        writer.set_strict(ctx.conn, "a/missing", True, "t", "op")
    with pytest.raises(writer.WriterError, match="no discovered_targets row"):
        writer.clear_tried(ctx.conn, "a/missing", "t")  # discovery pacing (2026-10-22): --untry
