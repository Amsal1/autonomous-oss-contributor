"""The /28 allocator's reserved tail (build-env design §4.6, N8): the last four /28s of every pool are never
handed out, so the floor-reach system test and the other root system tests own them; a row already there
keeps working."""

from __future__ import annotations

import ipaddress

import pytest

from ossbot.common.clock import to_iso
from ossbot.sandboxd.profiles import POOLS
from ossbot.sandboxd.subnets import RESERVED_TAIL, PoolExhausted, SubnetAllocator, pool_size, subnet_for
from tests.unit.sandboxd.conftest import Sbx

pytestmark = pytest.mark.unit


def _fill(alloc: SubnetAllocator, profile: str, n: int) -> list[str]:
    return [alloc.allocate(profile, f"c_{i:010d}") for i in range(n)]


def test_reserved_tail_is_four() -> None:
    assert RESERVED_TAIL == 4
    tail = [subnet_for(POOLS["fetch"], pool_size(POOLS["fetch"]) - i) for i in range(1, 5)]
    assert sorted(tail) == ["10.213.63.192/28", "10.213.63.208/28", "10.213.63.224/28", "10.213.63.240/28"]
    strict = subnet_for(POOLS["fetch-strict"], pool_size(POOLS["fetch-strict"]) - 2)
    assert strict == "10.213.223.224/28"  # the floor test's strict /28


@pytest.mark.parametrize("profile", ["fetch", "fetch-strict", "research"])
def test_the_last_four_are_never_allocated(sbx: Sbx, profile: str) -> None:
    alloc = SubnetAllocator(sbx.jobs.store, sbx.clock)
    usable = pool_size(POOLS[profile]) - RESERVED_TAIL
    got = _fill(alloc, profile, usable)
    assert len(set(got)) == usable
    reserved = {subnet_for(POOLS[profile], i) for i in range(usable, usable + RESERVED_TAIL)}
    assert not reserved & set(got)
    with pytest.raises(PoolExhausted):
        alloc.allocate(profile, "c_overflow00")
    assert all(ipaddress.ip_network(s).subnet_of(ipaddress.ip_network(POOLS[profile])) for s in got)


def test_an_existing_row_in_the_tail_is_still_returned(sbx: Sbx) -> None:
    pool = POOLS["fetch"]
    idx = pool_size(pool) - 1
    with sbx.jobs.store.tx() as conn:
        conn.execute("INSERT INTO subnets (profile, idx, cid, allocated_at, pool) VALUES (?, ?, ?, ?, ?)",
                     ("fetch", idx, "c_OLDROW0000", to_iso(sbx.clock.now()), pool))  # fmt: skip
    alloc = SubnetAllocator(sbx.jobs.store, sbx.clock)
    assert alloc.allocate("fetch", "c_OLDROW0000") == "10.213.63.240/28"  # nothing is moved
