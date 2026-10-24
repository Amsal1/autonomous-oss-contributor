"""Valid hold bodies (open-discovery design §13.3; ADR-0025 floor holds and egress notes) for gatekeeper,
Telegram and e2e tests."""

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


def floor_body(cls: str = "dst", target: str = "repo.maven.apache.org", dst: str = "10.0.0.1",
               **overrides: Any) -> dict[str, Any]:  # fmt: skip
    """A valid ``egress_floor`` hold (ADR-0025): a floor reach attempt, no volume breach."""
    body = hold_body(reason="egress_floor", breached=[], up_bytes=2 * MB, down_bytes=9 * MB)
    body["floor"] = {"class": cls, "target": target, "dst": dst}
    body.update(overrides)
    return body


def floor_request(cls: str = "dst", target: str = "repo.maven.apache.org", dst: str = "10.0.0.1",
                  **overrides: Any) -> HoldRequest:  # fmt: skip
    return HoldRequest.model_validate(floor_body(cls, target, dst, **overrides))


def note_body(**overrides: Any) -> dict[str, Any]:
    """A valid ``EgressNoteRequest`` (ADR-0025): a sandbox_full web job over its host and download limits."""
    body: dict[str, Any] = {
        "run_id": "validate-note01", "stage": "validate", "kind": "volume", "egress_mode": "sandbox_full",
        "hosts": 74, "up_bytes": 3 * MB, "down_bytes": 7372 * MB, "breached": ["hosts", "download"],
        "limits": {"max_hosts": 60, "max_upload_mb": 50, "max_download_mb": 6144},
        "top_hosts": ["repo.maven.apache.org:443", "services.gradle.org:443"], "notes": [],
    }  # fmt: skip
    body.update(overrides)
    return body
