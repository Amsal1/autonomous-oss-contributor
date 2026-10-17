"""Sandbox image pins read from a ``versions.lock.yaml`` copy (DESIGN §10.1, §10.8, §10.9).

The provision ``images`` step (FEAT-004) builds the images, writes their digests and the probe
inputs back into ``config/versions.lock.yaml`` and installs a root-owned copy at
``/etc/ossbot/versions.lock.yaml``; the Docker filter and sandboxd read only that copy::

    images:
      sandbox_full:
        digest: "sha256:<64 hex>"
      sandbox_llm:
        digest: "sha256:<64 hex>"
        kiro_cli_version: "2.32.0"
        agent_configs_sha256: "<64 hex>"
        settings_sha256: "<64 hex>"
      proxy:
        digest: "sha256:<64 hex>"

Missing, ``pending`` or malformed entries are simply absent (callers fail closed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ossbot.common.yamlio import YamlError, safe_load

DEFAULT_LOCK_COPY = Path("/etc/ossbot/versions.lock.yaml")
IMAGE_NAMES: tuple[str, ...] = ("sandbox-full", "sandbox-llm", "sandbox-android", "proxy")
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ImagePins:
    digests: dict[str, str]  # image name -> 64-hex digest (without the sha256: prefix)
    kiro_cli_version: str | None
    agent_configs_sha256: str | None
    settings_sha256: str | None

    def ref(self, image: str) -> str | None:
        digest = self.digests.get(image)
        return f"ossbot/{image}@sha256:{digest}" if digest else None


def _load(path: Path) -> dict[str, Any]:
    try:
        data = safe_load(path.read_bytes())
    except (OSError, YamlError, UnicodeDecodeError):
        return {}
    images = data.get("images") if isinstance(data, dict) else None
    return images if isinstance(images, dict) else {}


def _hex(value: Any) -> str | None:
    return value if isinstance(value, str) and _HEX.fullmatch(value) else None


def load_image_pins(path: Path = DEFAULT_LOCK_COPY) -> ImagePins:
    images = _load(path)
    digests: dict[str, str] = {}
    for key, entry in images.items():
        name = str(key).replace("_", "-")
        if name not in IMAGE_NAMES or not isinstance(entry, dict):
            continue
        digest = entry.get("digest")
        match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
        if match is not None:
            digests[name] = match.group(1)
    llm = images.get("sandbox_llm")
    llm = llm if isinstance(llm, dict) else {}
    version = llm.get("kiro_cli_version")
    return ImagePins(
        digests=digests,
        kiro_cli_version=version if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version) else None,
        agent_configs_sha256=_hex(llm.get("agent_configs_sha256")),
        settings_sha256=_hex(llm.get("settings_sha256")),
    )
