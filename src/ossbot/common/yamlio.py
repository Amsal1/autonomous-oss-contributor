"""YAML I/O with ruamel.yaml.

* ``safe_load`` for config validation: safe constructor, duplicate keys rejected.
* ``load_roundtrip`` / ``dump_roundtrip`` keep comments and ordering (models.yaml writer, §16.2).
"""

from __future__ import annotations

import io
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


class YamlError(ValueError):
    pass


def _safe() -> YAML:
    yaml = YAML(typ="safe", pure=True)
    yaml.allow_duplicate_keys = False
    return yaml


def _roundtrip() -> YAML:
    yaml = YAML(typ="rt")
    yaml.allow_duplicate_keys = False
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml


def safe_load(data: bytes | str) -> Any:
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    try:
        return _safe().load(text)
    except YAMLError as exc:
        raise YamlError(str(exc)) from exc


def load_roundtrip(text: str) -> Any:
    try:
        return _roundtrip().load(text)
    except YAMLError as exc:
        raise YamlError(str(exc)) from exc


def dump_roundtrip(data: Any) -> str:
    buf = io.StringIO()
    _roundtrip().dump(data, buf)
    return buf.getvalue()
