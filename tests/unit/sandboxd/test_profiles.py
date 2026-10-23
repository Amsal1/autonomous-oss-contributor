"""ADR-0024 profile tables: the -exec pool, its squid class and the jobs that must state their egress."""

from __future__ import annotations

import ipaddress

from ossbot.sandboxd.profiles import EGRESS_PROFILE, EGRESS_PROFILES, EGRESS_REQUIRED, POOLS, TEMPLATES


def test_exec_network_sits_in_pool_fetch_with_the_fetch_categories() -> None:
    assert POOLS["exec"] == POOLS["fetch"] == "10.213.0.0/18"
    assert EGRESS_PROFILE["exec"] == "fetch"
    strict = ipaddress.ip_network(POOLS["fetch-strict"])
    assert not ipaddress.ip_network(POOLS["exec"]).overlaps(strict)


def test_coder_must_state_its_egress_but_has_no_strict_network() -> None:
    assert sorted(EGRESS_REQUIRED) == ["coder", "fetch", "validate"]
    assert sorted(EGRESS_PROFILES) == ["fetch", "validate"]  # the <net>-strict suffix stays fetch/validate


def test_templates_unchanged_for_the_coder_pair() -> None:
    """agent-exec's template stays offline and key-free (the network is chosen per run); agent-llm unchanged."""
    exec_t, llm_t = TEMPLATES["agent-exec"], TEMPLATES["agent-llm"]
    assert (exec_t.network, exec_t.key, exec_t.image) == ("none", False, "sandbox-full")
    assert (llm_t.network, llm_t.key, llm_t.image, llm_t.llm_home) == ("llm", True, "sandbox-llm", True)
