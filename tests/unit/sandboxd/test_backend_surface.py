"""Every pipeline backend sits inside the unchanged sandbox boundary (ADR-0022): the same safety
probes, credential pairs the Docker filter already allows, and the per-backend key-scan list."""

from __future__ import annotations

import pytest

from ossbot.common.backends import KIRO_HEADLESS, PIPELINE_PROBES, BackendSpec, pipeline_backends
from ossbot.dockerfilter import rules
from ossbot.sandboxd import argv
from ossbot.sandboxd import main as main_mod
from ossbot.sandboxd.keyscan import KeyScanner
from ossbot.sandboxd.main import scan_secrets
from ossbot.sandboxd.probes import PROBES

KEY = "ksk_" + "k1" * 12
READ = "github_pat_" + "r2" * 12


@pytest.mark.parametrize("spec", pipeline_backends(), ids=lambda s: s.name)
def test_every_pipeline_backend_needs_all_probes(spec: BackendSpec) -> None:
    assert PIPELINE_PROBES == PROBES
    assert spec.required_probes == PROBES


@pytest.mark.parametrize("spec", pipeline_backends(), ids=lambda s: s.name)
def test_key_pairs_are_allowed_by_the_filter_and_sandboxd(spec: BackendSpec) -> None:
    pairs = spec.key_pairs()
    assert pairs <= rules.KEY_PAIRS and pairs <= argv.KEY_PAIRS
    for name in spec.credential_env:
        for profile, entry in pairs:
            rules._check_env([f"{name}={KEY}"], profile, entry)  # allowed: no Denied
        with pytest.raises(rules.Denied):
            rules._check_env([f"{name}={KEY}"], "code", "validate")  # repo code never gets it


def test_scan_list_is_todays_two_values() -> None:
    env = {"KIRO_API_KEY": KEY, "GITHUB_READ_TOKEN": READ, "OTHER": "x" * 20}
    assert scan_secrets(env) == [KEY, READ]
    assert scan_secrets({"KIRO_API_KEY": KEY}) == [KEY]
    assert scan_secrets({"KIRO_API_KEY": "", "GITHUB_READ_TOKEN": READ}) == [READ]
    assert scan_secrets({}) == []
    built, today = KeyScanner(scan_secrets(env)), KeyScanner([k for k in (KEY, READ) if k])
    assert built._needles == today._needles and built.hit(KEY[::-1].encode())


def test_scan_list_follows_the_backend_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """A future backend's credential is scanned once it is registered (the hook, not a new backend)."""
    monkeypatch.setattr(main_mod, "pipeline_credential_env", lambda: (*KIRO_HEADLESS.credential_env, "OTHER_KEY"))
    other = "other_" + "o3" * 12
    assert scan_secrets({"KIRO_API_KEY": KEY, "OTHER_KEY": other, "GITHUB_READ_TOKEN": READ}) == [KEY, other, READ]
