"""Engine selection for the Hermes provider profile (``HERMES_KIRO_ACP_ENGINE=v3|v2``).

``v3`` (default) drives ``kiro-cli acp --agent-engine=v3`` through ``kiro_acp.v3``; ``v2`` keeps the
original upstream path (``kiro_acp_client.KiroACPClient`` on Hermes' Copilot ACP shim) unchanged, as the
fallback while both server generations exist. The plugin's ``__init__.py`` calls into this module only
from ``create_client`` and ``fetch_models``.
"""

from __future__ import annotations

import os
from typing import Any

ENGINES = ("v3", "v2")


def engine() -> str:
    value = os.environ.get("HERMES_KIRO_ACP_ENGINE", "v3").strip().lower()
    return value if value in ENGINES else "v3"


def create_v3_client(**client_kwargs: Any) -> Any:
    from .v3 import KiroV3Client

    return KiroV3Client(**{k: v for k, v in client_kwargs.items() if k not in ("command", "args")})


def fetch_v3_models(timeout: float = 30.0) -> list[str] | None:
    from .v3 import list_models

    return list_models(timeout=timeout)
