"""Kiro output parsing (DESIGN §15.3): stream-json events -> assistant text -> the last fenced ```json
block -> a pydantic model (``extra=forbid``). LLM output is untrusted data: it is only ever validated,
stored and rendered escaped, never executed.

The V3 ACP stream (kiro-cli 2.32, verified live in FEAT-006 item 32) is read exactly (``_read_acp``:
final text, model in effect, agent fallback). Any other stream keeps the tolerant reader: it walks
every event for assistant text (``text``/``content``/``delta``), records the model the CLI reports
(``model``/``modelId``) and treats a "could not set model" warning as ``model_mismatch``. Non-JSON
lines count as plain text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

FENCE_RE = re.compile(r"```json[ \t]*\r?\n(.*?)\r?\n?```", re.DOTALL)
MISMATCH_RE = re.compile(r"(could not|cannot|unable to|failed to) (set|use|switch)[^\n]{0,40}model", re.IGNORECASE)
TEXT_KEYS = ("text", "content", "delta")
MODEL_KEYS = ("model", "modelId", "model_id")
MAX_TEXT = 2_000_000


class OutputInvalid(ValueError):
    """No parseable ```json block, or it failed the schema; ``reason`` feeds the repair prompt."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class KiroOutput:
    text: str
    model_actual: str | None = None
    model_mismatch: bool = False
    tool_calls: int = 0
    warnings: list[str] = field(default_factory=list)
    agent_fallback: bool = False  # the CLI ignored our role agent and ran its default agent


def _texts(node: Any, out: list[str], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key in TEXT_KEYS and isinstance(value, str):
                out.append(value)
            elif isinstance(value, dict | list):
                _texts(value, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _texts(item, out, depth + 1)


def _find(node: Any, keys: tuple[str, ...], depth: int = 0) -> str | None:
    if depth > 6:
        return None
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
        for value in node.values():
            found = _find(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find(item, keys, depth + 1)
            if found:
                return found
    return None


AGENT_FALLBACK_RE = re.compile(r'agent \\?"ossbot-[a-z]+\\?"[^\n]{0,200}using \\?"default\\?"')


def _is_acp(raw: str) -> bool:
    for line in raw.splitlines()[:5]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "runStarted":
            data = event.get("data") or {}
            return isinstance(data, dict) and data.get("payloadSchema") == "acp"
    return False


def _read_acp(raw: str, stderr: str) -> KiroOutput:
    """kiro-cli 2.32 ``chat --v3 --output-format stream-json`` (recorded live, FEAT-006 item 32):
    the answer is ``runFinished.data.finalText`` (agent message chunks as a fallback), the model in
    effect is the last ``configOptions[id=model].currentValue``, and a ``runError`` saying our agent
    was replaced by ``default`` means the role ran without its tool restrictions: ``agent_fallback``."""
    out = KiroOutput(text="")
    chunks: list[str] = []
    final: str | None = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind, data = str(event.get("type", "")), event.get("data")
        data = data if isinstance(data, dict) else {}
        if kind == "runFinished" and isinstance(data.get("finalText"), str):
            final = data["finalText"]
        elif kind == "runError":
            message = str(data.get("message", ""))[:500]
            out.warnings.append(message)
            if AGENT_FALLBACK_RE.search(message):
                out.agent_fallback = True
            if MISMATCH_RE.search(message):
                out.model_mismatch = True
        update = data.get("update") if kind == "sessionUpdate" else None
        if not isinstance(update, dict):
            continue
        su = update.get("sessionUpdate")
        if su == "tool_call":
            out.tool_calls += 1
        elif su == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
        for option in update.get("configOptions") or []:
            if isinstance(option, dict) and option.get("id") == "model" and isinstance(option.get("currentValue"), str):
                out.model_actual = option["currentValue"][:128]
    out.text = (final if final is not None else "".join(chunks))[:MAX_TEXT]
    if MISMATCH_RE.search(stderr):
        out.model_mismatch = True
    return out


def read_events(raw: bytes, stderr: bytes = b"") -> KiroOutput:
    text = raw.decode("utf-8", "replace")
    if _is_acp(text):
        return _read_acp(text, stderr.decode("utf-8", "replace"))
    parts: list[str] = []
    out = KiroOutput(text="")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            parts.append(raw_line + "\n")
            continue
        kind = str(event.get("type", "")) if isinstance(event, dict) else ""
        if "tool" in kind.lower() and ("use" in kind.lower() or "call" in kind.lower()):
            out.tool_calls += 1
        if "warn" in kind.lower() or "error" in kind.lower():
            message = json.dumps(event)[:500]
            out.warnings.append(message)
            if MISMATCH_RE.search(message):
                out.model_mismatch = True
            continue
        model = _find(event, MODEL_KEYS)
        if model:
            out.model_actual = model[:128]
        if kind and "user" in kind.lower():
            continue  # our own prompt echoed back is not assistant text
        _texts(event, parts)
    out.text = "".join(parts)[:MAX_TEXT]  # streamed deltas concatenate; whole messages keep their newlines
    err = stderr.decode("utf-8", "replace")
    if MISMATCH_RE.search(err):
        out.model_mismatch = True
        out.warnings.append(err[:500])
    return out


def last_json_block(text: str) -> Any:
    blocks = FENCE_RE.findall(text)
    if not blocks:
        raise OutputInvalid("no ```json block found")
    try:
        return json.loads(blocks[-1])
    except ValueError as exc:
        raise OutputInvalid(f"the last ```json block is not valid JSON: {exc}") from exc


def parse_model[T: BaseModel](text: str, model: type[T]) -> T:
    data = last_json_block(text)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:10])
        raise OutputInvalid(f"schema errors: {errors}") from exc


def repair_prompt(reason: str, schema_hint: str) -> str:
    """The one repair retry: fixed text plus the validator's reason (no model text is echoed)."""
    return (
        "Your previous answer could not be used: " + reason[:800] + "\n"
        "Answer again with exactly one fenced ```json block that matches this schema and nothing else:\n" + schema_hint
    )
