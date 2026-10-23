"""ADR-0024 amends I5: DESIGN §8.3 and the AGENTS.md invariant table carry the same new wording (R2.14)."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRAGMENTS = (
    "`agent-exec` when the owner's `implement_network` allows it (`auto`, or `on_request` with a human rerun grant)",
    "their `agent-exec` never gets a network",
    "every fetch/validate/coder job states its egress",
    "Repo code runs only in containers without any credential; `agent-llm` and `agent-exec` sit on separate"
    " per-task networks.",
    "never get egress beyond their own profile's mode list",
)

pytestmark = pytest.mark.unit


def _i5_row(path: Path) -> str:
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("| I5 |")]
    assert len(rows) == 1, path
    return rows[0]


@pytest.mark.parametrize("doc", ["docs/DESIGN.md", "AGENTS.md"])
@pytest.mark.parametrize("fragment", FRAGMENTS)
def test_i5_carries_the_adr_0024_wording(doc: str, fragment: str) -> None:
    assert fragment in _i5_row(ROOT / doc)


@pytest.mark.parametrize("doc", ["docs/DESIGN.md", "AGENTS.md"])
def test_i5_names_both_adrs(doc: str) -> None:
    row = _i5_row(ROOT / doc)
    assert "ADR-0023" in row and "ADR-0024" in row
