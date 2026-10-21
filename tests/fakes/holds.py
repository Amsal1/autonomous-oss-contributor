"""A valid egress-anomaly ``HoldRequest`` (open-discovery design §13.3) for gatekeeper, Telegram and e2e tests."""

from __future__ import annotations

from typing import Any

from ossbot.common.schemas import HoldRequest

MB = 1024 * 1024


def hold_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "reason": "egress_anomaly", "run_id": "validate-abc123", "stage": "validate", "egress": "mode",
        "egress_mode": "sandbox_open", "hosts": 14, "up_bytes": 312 * MB, "down_bytes": 1200 * MB, "denied": 3,
        "top_hosts": ["pypi.org:443", "files.pythonhosted.org:443", "paste.example.net:443"],
        "limits": {"max_hosts": 60, "max_upload_mb": 50, "max_download_mb": 6144}, "breached": ["upload"],
    }  # fmt: skip
    body.update(overrides)
    return body


def hold_request(**overrides: Any) -> HoldRequest:
    return HoldRequest.model_validate(hold_body(**overrides))
