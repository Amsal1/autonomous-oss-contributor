"""Owner decision 2026-10-18: model ids live only in config/models.yaml. Code, scripts, probes, agents
and prompts read them through the resolver (``roles.<r>``), so switching a role, including
``roles.test`` for every live check, never needs a code change."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src", "scripts", "sandbox", "prompts", ".kiro")
MODEL_ID = re.compile(r"\b(claude|gpt|glm|deepseek|minimax|qwen\d*|gemini|llama|mistral|haiku|sonnet|opus)"
                      r"-(\d|coder|next|opus|sonnet|haiku)[\w.-]*", re.IGNORECASE)  # fmt: skip
SUFFIXES = {".py", ".sh", ".ps1", ".psm1", ".md", ".json", ".yaml", ".yml", ".j2", ".toml", ".txt", ""}


def test_no_model_id_outside_models_yaml() -> None:
    hits: list[str] = []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*")):
            if not path.is_file() or path.suffix not in SUFFIXES or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for n, line in enumerate(text.splitlines(), start=1):
                if MODEL_ID.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert hits == [], "model ids belong in config/models.yaml only:\n" + "\n".join(hits)


def test_the_scan_would_catch_one() -> None:
    assert MODEL_ID.search('model = "claude-haiku-4.5"') and MODEL_ID.search("gpt-5.6-sol")
    assert not MODEL_ID.search("ossbot-runner.service") and not MODEL_ID.search("python-3.12")
