"""The pinned OSI license list (design §5.2, §15 V5): count, samples, header and the lock entry."""

from __future__ import annotations

import pytest

from ossbot.common import osi_licenses
from ossbot.common.osi_licenses import OSI_SPDX_IDS, is_osi
from ossbot.common.yamlio import safe_load
from tests.conftest import REPO_ROOT


def test_count_matches_the_pinned_list() -> None:
    assert len(OSI_SPDX_IDS) == 154


@pytest.mark.parametrize("spdx", ["MIT", "Apache-2.0", "MPL-2.0", "BSD-3-Clause", "GPL-2.0", "ISC", "0BSD"])
def test_osi_ids_pass(spdx: str) -> None:
    assert is_osi(spdx)


@pytest.mark.parametrize("spdx", ["BUSL-1.1", "SSPL-1.0", "Elastic-2.0", "NOASSERTION", None, "", "mit"])
def test_non_osi_ids_fail(spdx: str | None) -> None:
    assert not is_osi(spdx)


def test_header_names_version_sha_and_the_lock_agrees() -> None:
    doc = osi_licenses.__doc__ or ""
    assert "3.29.0" in doc and osi_licenses.SPDX_LIST_SHA256 in doc
    lock = safe_load((REPO_ROOT / "config" / "versions.lock.yaml").read_bytes())["spdx_license_list"]
    assert lock["version"] == osi_licenses.SPDX_LIST_VERSION
    assert lock["sha256"] == osi_licenses.SPDX_LIST_SHA256
    assert lock["url"].endswith("/v3.29.0/json/licenses.json") and lock["osi_ids"] == "154"
