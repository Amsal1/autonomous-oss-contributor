"""``config/telegram.yaml`` and the profile's Telegram fields (DESIGN §14.20.3, §14.20.6, AC 25)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ossbot.common.config import ConfigError, TelegramConfig, parse_bundle, parse_yaml_model

ROOT = Path(__file__).resolve().parents[3]


def _files() -> dict[str, bytes]:
    files = {n: (ROOT / "config" / n).read_bytes() for n in ("policy.yaml", "egress.yaml", "targets.yaml",
                                                               "telegram.yaml")}  # fmt: skip
    files["profile.yaml"] = (ROOT / "tests" / "fixtures" / "profile.test.yaml").read_bytes()
    return files


def test_tracked_file_parses_with_the_defaults() -> None:
    bundle = parse_bundle(_files())
    assert bundle.telegram == TelegramConfig()
    assert bundle.telegram.review_mirror.enabled is False and bundle.telegram.telegraph.enabled is False


def test_all_text_mode_and_command_shortcuts_defaults_and_values() -> None:
    cfg = TelegramConfig()
    assert (cfg.all_text_mode, cfg.command_shortcuts) == ("topic_session", True)
    for mode in ("topic_session", "legacy"):
        for flag in ("true", "false"):
            data = f"version: 1\nall_text_mode: {mode}\ncommand_shortcuts: {flag}\n".encode()
            got = parse_yaml_model(TelegramConfig, "telegram.yaml", data)
            assert (got.all_text_mode, got.command_shortcuts) == (mode, flag == "true")


def test_missing_file_means_defaults() -> None:
    files = _files()
    del files["telegram.yaml"]
    assert parse_bundle(files).telegram == TelegramConfig()


@pytest.mark.parametrize("bad", [
    "version: 1\nbogus: 1\n",
    "version: 1\nloud_cards_per_hour: 21\n",
    "version: 1\ndashboard: {min_edit_interval_s: 10}\n",
    "version: 1\ndrafts: {heartbeat_s: 30}\n",
    "version: 1\ndigest: {daily: '25:00'}\n",
    "version: 1\ndigest: {weekly: 'Funday 18:00'}\n",
    "version: 1\ntopics: on\n",
    "version: 2\n",
    "version: 1\ntelegraph: {max_content_kb: 64}\n",
    "version: 1\nall_text_mode: flat\n",
    "version: 1\ncommand_shortcuts: maybe\n",
])  # fmt: skip
def test_unknown_keys_and_out_of_range_values_are_refused(bad: str) -> None:
    with pytest.raises(ConfigError):
        parse_yaml_model(TelegramConfig, "telegram.yaml", bad.encode())
    files = _files()
    files["telegram.yaml"] = bad.encode()
    with pytest.raises(ConfigError) as err:
        parse_bundle(files)
    assert "telegram.yaml" in err.value.errors


def _profile(extra: str) -> bytes:
    text = (ROOT / "tests" / "fixtures" / "profile.test.yaml").read_text(encoding="utf-8")
    return text.replace("  user_id: 4242\n", f"  user_id: 4242\n{extra}").encode()


def test_profile_timezone_and_quiet_hours() -> None:
    files = _files()
    files["profile.yaml"] = _profile("  timezone: Asia/Bangkok\n  quiet_hours: '23:00-07:30'\n")
    tg = parse_bundle(files).profile.telegram
    assert (tg.timezone, tg.quiet_hours) == ("Asia/Bangkok", "23:00-07:30")
    for bad in ("  timezone: Mars/Olympus\n", "  quiet_hours: '23-07'\n", "  quiet_hours: '24:00-01:00'\n"):
        files["profile.yaml"] = _profile(bad)
        with pytest.raises(ConfigError):
            parse_bundle(files)
