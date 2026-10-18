"""Doctor P1-17 check: no service uid lingers or has a /run/user runtime dir."""

from __future__ import annotations

import pwd
from pathlib import Path

import pytest

from ossbot.ctl import doctor


class _Pw:
    def __init__(self, uid: int) -> None:
        self.pw_uid = uid


def test_clean_and_dirty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uids = {"ossrunner": 990, "hermes": 991, "gatekeeper": 992, "ossbox": 993, "ossnobody": 1001}
    monkeypatch.setattr(pwd, "getpwnam", lambda name: _Pw(uids[name]))
    linger, run_user = tmp_path / "linger", tmp_path / "run_user"
    linger.mkdir()
    run_user.mkdir()
    (run_user / "1001").mkdir()  # the default WSL login's session dir is expected (no unit runs as it)
    assert doctor.check_no_user_managers(linger, run_user).status == doctor.PASS
    (linger / "hermes").touch()
    (run_user / "992").mkdir()
    result = doctor.check_no_user_managers(linger, run_user)
    assert result.status == doctor.FAIL and "hermes lingers" in result.detail and "/run/user/992" in result.detail
