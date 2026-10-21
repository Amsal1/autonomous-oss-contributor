"""Doctor check of the optional SSH commit-signing key (ADR-0006 addendum): present and usable (sign +
``git verify-commit`` on a throw-away commit), and listed among the login's GitHub signing keys."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from ossbot.ctl import doctor
from ossbot.gatekeeper import signing
from tests.fakes.signing_key import key_body, needs_keygen

pytestmark = [pytest.mark.unit, needs_keygen]


class _Resp:
    def __init__(self, status: int, doc: Any) -> None:
        self.status_code = status
        self._doc = doc

    def json(self) -> Any:
        return self._doc


def _setup(monkeypatch: pytest.MonkeyPatch, body: str | None, keys: list[dict[str, str]] | None = None) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr(doctor, "_is_root", lambda: True)
    monkeypatch.setattr(doctor, "env_value", lambda file, key: body if (file, key) == (
        "gatekeeper.env", signing.SIGNING_ENV) else None)  # fmt: skip
    monkeypatch.setattr(doctor, "_top_profile_value", lambda key: "test-bot-login")

    def get(url: str, **kw: Any) -> _Resp:
        urls.append(url)
        return _Resp(200, keys or [])

    monkeypatch.setattr(httpx, "get", get)
    return urls


def test_no_key_is_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(monkeypatch, None)
    (result,) = doctor.check_commit_signing(quick=False)
    assert result.status == doctor.SKIP


def test_key_listed_on_github_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = key_body(tmp_path)
    public = (tmp_path / "sign.pub").read_text(encoding="ascii").strip()
    urls = _setup(monkeypatch, body, [{"key": public, "title": "ossbot"}])
    local, remote = doctor.check_commit_signing(quick=False)
    assert local.status == doctor.PASS and "signed and verified" in local.detail
    assert remote.status == doctor.PASS
    assert urls == ["https://api.github.com/users/test-bot-login/ssh_signing_keys?per_page=100"]
    assert all(line not in local.detail + remote.detail for line in signing.key_needles(body))


def test_key_missing_on_github_fails_with_the_settings_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = key_body(tmp_path)
    other = tmp_path / "other.pub"
    key_body(tmp_path, "other")
    _setup(monkeypatch, body, [{"key": other.read_text(encoding="ascii").strip()}])
    local, remote = doctor.check_commit_signing(quick=False)
    assert local.status == doctor.PASS
    assert remote.status == doctor.FAIL and "Signing Key" in remote.hint


def test_quick_skips_github_and_a_broken_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    urls = _setup(monkeypatch, "QUJD" * 40)
    (result,) = doctor.check_commit_signing(quick=True)
    assert result.status == doctor.FAIL
    assert urls == []
