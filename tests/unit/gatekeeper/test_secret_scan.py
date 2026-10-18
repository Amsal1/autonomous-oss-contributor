"""The U14 secret scanner (DESIGN §14.9, BA-16, BA-21): values, patterns, Kiro key, load refusals."""

from __future__ import annotations

import pytest

from ossbot.common.config import ConfigError
from ossbot.common.hashing import sha256_text
from ossbot.common.redact import REDACTED
from ossbot.gatekeeper import secret_scan
from ossbot.gatekeeper.context import AppContext
from ossbot.gatekeeper.secret_scan import HOLD_BACK, SecretScanError, build, can_match_whitespace

HERMES_KEY = "hk_" + "A1b2C3d4E5f6G7h8I9j0K1l2"  # assembled at runtime: no secret-shaped literal in git
GHP = "ghp_" + "Q8xk2LmZ9vR4tYp7" + "Wn3bJc6Hd1Fs5Ga0Ke8U"
KIRO = "kiro-dedicated-key-value-0123456789"


def test_values_patterns_and_kiro_key(ctx: AppContext) -> None:
    env = {"HERMES_API_KEY": HERMES_KEY, "GITHUB_WRITE_TOKEN": "short", "KIRO_KEY_SHA256": sha256_text(KIRO)}
    scanner = build(ctx.config, env)
    assert scanner.values == frozenset({HERMES_KEY})  # < 8 characters is ignored
    text = f"a {HERMES_KEY} b {GHP} c {KIRO} d"
    assert scanner.scan(text) == 3 and scanner.scan(text.encode()) == 3
    assert scanner.value_hits(text) == 1
    redacted, hits = scanner.redact(text)
    assert hits == 3 and HERMES_KEY not in redacted and GHP not in redacted and KIRO not in redacted
    assert redacted.count(REDACTED) == 3
    assert scanner.scan("nothing here") == 0


@pytest.mark.parametrize(
    "env",
    [{"HERMES_API_KEY": "has a space-in-it"}, {"OSSBOT_TOKEN_PEPPER": "x" * HOLD_BACK},
     {"TELEGRAPH_ACCESS_TOKEN": "tab\there-long-value"}],
)  # fmt: skip
def test_refused_secret_values(ctx: AppContext, env: dict[str, str]) -> None:
    with pytest.raises(SecretScanError):
        build(ctx.config, env)


@pytest.mark.parametrize(
    ("pattern", "matches_ws"),
    [
        (r"ksk_[A-Za-z0-9_\-]{16,}", False), (r"gh[pousr]_[A-Za-z0-9]{36,}", False), (r"\d{6,12}:[\w-]{30,}", False),
        (r"secret.+", True), (r"key\s*=", True), (r"[^a]{20}", True), (r"a\Wb", True), (r"x\D", True),
        (r"a b", True), (r"(?:a|\t)", True), (r"[\x00-\x20]+", True), (r"\u00a0", True), (r"[\S]{9}", False),
        (r"(?=x)abc", False), (r"tok(en)?_[a-z]{8}", False),
    ],
)  # fmt: skip
def test_whitespace_table(pattern: str, matches_ws: bool) -> None:
    assert can_match_whitespace(pattern) is matches_ws


def test_whitespace_pattern_refused_at_load_and_reload(ctx: AppContext) -> None:
    bundle = ctx.config
    secrets = bundle.policy.secrets.model_copy(update={"extra_patterns": ["password=.+"]})
    policy = bundle.policy.model_copy(update={"secrets": secrets})
    bad = type(bundle)(policy, bundle.targets, bundle.egress, bundle.profile)
    with pytest.raises(SecretScanError, match="whitespace"):
        build(bad, {})
    path = ctx.paths.config_dir / "policy.yaml"
    path.write_text(path.read_text().replace('extra_patterns: ["', 'extra_patterns: ["pass=.+", "'))
    with pytest.raises(ConfigError, match="whitespace"):
        ctx.snapshots.prepare_reload()


def test_wide_pattern_refused(ctx: AppContext) -> None:
    secrets = ctx.config.policy.secrets.model_copy(update={"extra_patterns": [f"Z{{{HOLD_BACK}}}"]})
    policy = ctx.config.policy.model_copy(update={"secrets": secrets})
    bundle = type(ctx.config)(policy, ctx.config.targets, ctx.config.egress, ctx.config.profile)
    with pytest.raises(SecretScanError, match="minimum match"):
        build(bundle, {})


def test_context_scanner_follows_the_snapshot(ctx: AppContext) -> None:
    ctx.env = {"HERMES_API_KEY": HERMES_KEY}
    ctx.reset_secret_scanner()
    first = ctx.secret_scanner
    assert first.values == frozenset({HERMES_KEY}) and ctx.secret_scanner is first  # cached per snapshot
    path = ctx.paths.config_dir / "policy.yaml"
    path.write_text(path.read_text().replace("comments_per_24h: 10", "comments_per_24h: 9"))
    ctx.snapshots.apply_reload(ctx.snapshots.prepare_reload().hash, "tg:4242")
    assert ctx.secret_scanner is not first  # rebuilt on a confirmed /reload
    assert secret_scan.SECRET_ENV_KEYS[:4] == ("TELEGRAM_BOT_TOKEN", "GITHUB_WRITE_TOKEN", "HERMES_API_KEY",
                                                "OSSBOT_TOKEN_PEPPER")  # fmt: skip
