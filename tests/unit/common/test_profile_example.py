"""``config/profile.example.yaml`` (B7, AC 23): loads with ProfileConfig, covers every profile key and
holds only placeholders. This is CI's profile check (CI has no real profile)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ossbot.common.config import ProfileConfig
from ossbot.common.yamlio import safe_load

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "config" / "profile.example.yaml"
PLACEHOLDER_HOST = re.compile(r"(^|[.@])example(\.[a-z]+)?$")


def _raw() -> dict[str, Any]:
    data = safe_load(EXAMPLE.read_bytes())
    assert isinstance(data, dict)
    return data


def test_example_loads() -> None:
    ProfileConfig.model_validate(_raw())


def _keys(model: type[BaseModel], prefix: str = "") -> set[str]:
    out: set[str] = set()
    for name, field in model.model_fields.items():
        out.add(prefix + name)
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out |= _keys(ann, f"{prefix}{name}.")
    return out


def _present(data: dict[str, Any], prefix: str = "") -> set[str]:
    out: set[str] = set()
    for key, value in data.items():
        out.add(prefix + key)
        if isinstance(value, dict) and key != "categories_priority":
            out |= _present(value, f"{prefix}{key}.")
    return out


def test_example_names_every_profile_key() -> None:
    missing = _keys(ProfileConfig) - _present(_raw())
    assert not missing, f"add these keys to config/profile.example.yaml: {sorted(missing)}"


def test_example_holds_only_placeholders() -> None:
    p = ProfileConfig.model_validate(_raw())
    assert p.github_login.startswith("your-")
    assert p.commit_name == "Your Name"
    assert PLACEHOLDER_HOST.search(p.commit_email.split("@", 1)[1])
    assert all(x.startswith("your-") for x in p.forbidden_logins)
    assert all(x.startswith("your-") or PLACEHOLDER_HOST.search(x.lstrip("@")) for x in p.confidentiality_floor)
    assert p.telegram.user_id == 0
    assert p.telegram.quiet_hours is None
    host = p.kiro.idc_start_url.split("/")[2]
    assert PLACEHOLDER_HOST.search(host.split(".awsapps.com")[0])
    assert all("replace" in r for r in p.backup.age_recipients)
    assert p.review_mirror.repo is None
    assert p.interests.prior_contributions == []
    assert p.interests.summary.startswith("Describe")
