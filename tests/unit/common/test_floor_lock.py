"""The root-only public-floor lock (carried criterion C-M1, build-env design §4.7).

The live owner is root; these hermetic cases run as the test user with ``owner``/``group`` set to it (the
only override, by argument). ``tests/system/test_floor_lock.sh`` proves the live file as root and that
ossbox and gatekeeper can't create, open or hold it."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from ossbot.common.floor_lock import LOCK_PATH, FloorLockBusy, FloorLockUntrusted, floor_lock

pytestmark = pytest.mark.unit
ME = {"owner": os.geteuid(), "group": os.getegid()}


@pytest.fixture
def floor(tmp_path: Path) -> Path:
    d = tmp_path / "floor"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)  # noqa: S103 - the live floor directory's mode (root-only writable, world-readable)
    return d


def test_live_path_is_in_the_root_only_floor_directory() -> None:
    assert Path("/etc/ossbot/proxy/floor/.lock") == LOCK_PATH
    assert not str(LOCK_PATH).startswith("/run/")


def test_creates_the_lock_0600_and_takes_it(floor: Path) -> None:
    with floor_lock(True, 1.0, floor / ".lock", **ME):
        st = (floor / ".lock").stat()
        assert oct(st.st_mode & 0o777) == "0o600" and st.st_uid == ME["owner"]
    with floor_lock(False, 1.0, floor / ".lock", **ME):  # existing trusted file: reused
        pass


def test_creates_a_missing_directory_root_only(tmp_path: Path) -> None:
    d = tmp_path / "new-floor"
    with floor_lock(True, 1.0, d / ".lock", **ME):
        pass
    assert oct(d.stat().st_mode & 0o777) == "0o755"


@pytest.mark.parametrize("case", ["mode_0644", "mode_0640", "symlink", "fifo", "directory", "hardlink", "other_uid"])
def test_untrusted_lock_file_is_refused(floor: Path, case: str, caplog: pytest.LogCaptureFixture) -> None:
    lock = floor / ".lock"
    if case.startswith("mode_"):
        lock.write_text("")
        os.chmod(lock, int(case[-4:], 8))
    elif case == "symlink":
        target = floor.parent / "elsewhere"
        target.write_text("")
        os.chmod(target, 0o600)
        lock.symlink_to(target)
    elif case == "fifo":
        os.mkfifo(lock, 0o600)
    elif case == "directory":
        lock.mkdir()
    elif case == "hardlink":
        lock.write_text("")
        os.chmod(lock, 0o600)
        os.link(lock, floor.parent / "second-name")
    before = sorted(p.name for p in floor.iterdir())
    kwargs = dict(ME)
    if case == "other_uid":  # a file owned by another uid (simulated: we expect a different owner)
        lock.write_text("")
        os.chmod(lock, 0o600)
        before = sorted(p.name for p in floor.iterdir())
        kwargs = {"owner": ME["owner"] + 1, "group": ME["group"]}
    with caplog.at_level("ERROR"), pytest.raises(FloorLockUntrusted) as info, floor_lock(True, 1.0, lock, **kwargs):
        pytest.fail("an untrusted lock must never be held")
    assert str(info.value).startswith("floor_lock_untrusted: ")
    assert any(getattr(r, "event", None) == "floor_lock_untrusted" for r in caplog.records)
    assert sorted(p.name for p in floor.iterdir()) == before  # nothing created, fixed or removed


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777])
def test_group_or_other_writable_directory_is_refused(floor: Path, mode: int) -> None:
    os.chmod(floor, mode)
    with pytest.raises(FloorLockUntrusted, match="writable"), floor_lock(False, 1.0, floor / ".lock", **ME):
        pass
    assert not (floor / ".lock").exists()


def test_directory_of_another_owner_or_a_symlinked_directory_is_refused(floor: Path, tmp_path: Path) -> None:
    other = {"owner": ME["owner"] + 1, "group": ME["group"]}
    with (
        pytest.raises(FloorLockUntrusted, match="directory owned by"),
        floor_lock(False, 1.0, floor / ".lock", **other),
    ):
        pass
    link = tmp_path / "linked"
    link.symlink_to(floor)
    with pytest.raises(FloorLockUntrusted, match="not a real directory"), floor_lock(False, 1.0, link / ".lock", **ME):
        pass


def test_exclusive_blocks_shared_until_released_then_times_out_busy(floor: Path) -> None:
    lock = floor / ".lock"
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with floor_lock(True, 1.0, lock, **ME):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    started = time.monotonic()
    with pytest.raises(FloorLockBusy, match="held by another process"), floor_lock(False, 0.3, lock, **ME):
        pass
    assert time.monotonic() - started >= 0.3
    release.set()
    t.join(5)
    with floor_lock(False, 1.0, lock, **ME):  # free again
        pass


def test_shared_holders_coexist(floor: Path) -> None:
    lock = floor / ".lock"
    with floor_lock(False, 1.0, lock, **ME), floor_lock(False, 0.2, lock, **ME):
        pass
    with pytest.raises(FloorLockBusy), floor_lock(False, 1.0, lock, **ME), floor_lock(True, 0.2, lock, **ME):
        pass
