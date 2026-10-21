"""Fix-round unbundle input (DESIGN §9.8, ADR-0006 addendum): a signed PR head comes from the
gatekeeper's signed bundle (``GET head-bundle``), an unsigned one from the rev's artifact bundle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ossbot.runner.stages import implement

pytestmark = pytest.mark.unit
CID = "c_0123456789"
HEAD = "a" * 40


class _Gk:
    def __init__(self, data: bytes | None) -> None:
        self.data = data
        self.calls: list[tuple[str, int]] = []

    def head_bundle(self, cid: str, rev: int) -> bytes | None:
        self.calls.append((cid, rev))
        return self.data


class _Sc:
    def __init__(self, root: Path, data: bytes | None) -> None:
        self.inputs_root = root / "runner" / "inputs"
        self.gk = _Gk(data)


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: bytes | None) -> tuple[_Sc, dict[str, Any]]:
    seen: dict[str, Any] = {}

    def job(sc: Any, cid: str, stage: str, profile: str, entrypoint: str, args: dict[str, Any], mode: str,
            **kw: Any) -> dict[str, Any]:  # fmt: skip
        seen.update(args=args, inputs=kw["inputs"], entrypoint=entrypoint)
        return {}

    monkeypatch.setattr(implement, "job", job)
    sc = _Sc(tmp_path, data)
    implement._unbundle(sc, {"id": CID, "artifact_rev": 2, "head_sha": HEAD})  # type: ignore[arg-type]
    return sc, seen


def test_unsigned_head_uses_the_artifact_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sc, seen = _run(monkeypatch, tmp_path, None)
    assert sc.gk.calls == [(CID, 2)]
    assert seen["inputs"] == {"change.bundle": str(tmp_path / "artifacts" / CID / "2" / "change.bundle")}
    assert seen["args"]["checkout_sha"] == HEAD and seen["entrypoint"] == "unbundle"


def test_signed_head_uses_the_gatekeeper_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, seen = _run(monkeypatch, tmp_path, b"# v2 git bundle\n")
    path = Path(seen["inputs"]["change.bundle"])
    assert path.read_bytes() == b"# v2 git bundle\n"
    assert path.is_relative_to(tmp_path / "runner" / "inputs" / CID)
    assert seen["args"] == {"mode": "fix_round", "bundle_ref": "refs/heads/ossbot-change", "checkout_sha": HEAD,
                            "rev": 2}  # fmt: skip
