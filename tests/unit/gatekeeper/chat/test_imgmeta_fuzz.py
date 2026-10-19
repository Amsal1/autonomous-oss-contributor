"""Fuzz: whatever bytes arrive, the walker returns a stripped image or raises ImageRejected only."""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ossbot.gatekeeper.chat import imgmeta
from tests.unit.gatekeeper.chat import images

SEEDS = [images.jpeg(), images.png(), images.webp(), images.heic()]


def _check(data: bytes) -> None:
    try:
        out = imgmeta.strip(data)
    except imgmeta.ImageRejected:
        return
    assert out.mime in ("image/jpeg", "image/png", "image/webp")
    assert len(out.data) <= len(data) + 16


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.binary(max_size=2048))
def test_random_bytes(data: bytes) -> None:
    _check(data)


@settings(max_examples=600, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.sampled_from(SEEDS), st.lists(st.tuples(st.integers(0, 4000), st.integers(0, 255)), max_size=12),
       st.integers(0, 4000))  # fmt: skip
def test_mutated_containers(seed: bytes, flips: list[tuple[int, int]], cut: int) -> None:
    data = bytearray(seed)
    for pos, value in flips:
        if data:
            data[pos % len(data)] = value
    _check(bytes(data[: max(1, len(data) - cut % max(1, len(data)))]))
