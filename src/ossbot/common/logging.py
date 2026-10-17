"""JSON-lines logging with a redaction hook (DESIGN §21).

Fields: ts, level, component, event, cid, run_id, stage, msg, extra.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from ossbot.common.redact import Redactor

_CONTEXT_FIELDS = ("event", "cid", "run_id", "stage")
_STD_ATTRS = set(logging.LogRecord("x", 0, "x", 0, "x", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str, redactor: Redactor | None = None) -> None:
        super().__init__()
        self.component = component
        self.redactor = redactor or Redactor()

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "component": self.component,
            "logger": record.name,
        }
        for field in _CONTEXT_FIELDS:
            value = record.__dict__.get(field)
            if value is not None:
                doc[field] = value
        doc["msg"] = record.getMessage()
        extra = {k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and k not in _CONTEXT_FIELDS}
        if extra:
            doc["extra"] = extra
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return self.redactor.redact(json.dumps(doc, default=str, ensure_ascii=False))


def setup_logging(component: str, redactor: Redactor | None = None, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(component, redactor))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
