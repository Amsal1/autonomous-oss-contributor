"""/28 allocation per task network (DESIGN §10.3): first free index in the profile's pool,
persisted in ``subnets(profile, idx, cid, allocated_at, pool)``; profiles sharing a pool never
collide because the primary key is ``(pool, idx)``."""

from __future__ import annotations

import ipaddress

from ossbot.common.clock import Clock, to_iso
from ossbot.sandboxd.db import Store
from ossbot.sandboxd.profiles import POOLS

PREFIX = 28
# The last four /28s of every pool are never handed out (build-env design §4.6, N8): the floor-reach system
# test and the other root system tests use them for their own networks, so a test can never share a job's
# /28 or be attributed to a job. An existing row there (allocated before) keeps working; nothing is moved.
RESERVED_TAIL = 4


class PoolExhausted(RuntimeError):
    pass


def subnet_for(pool: str, idx: int) -> str:
    net = ipaddress.IPv4Network(pool)
    size = 2 ** (PREFIX - net.prefixlen)
    if not 0 <= idx < size:
        raise ValueError("index outside pool")
    base = int(net.network_address) + idx * 2 ** (32 - PREFIX)
    return f"{ipaddress.IPv4Address(base)}/{PREFIX}"


def pool_size(pool: str) -> int:
    return int(2 ** (PREFIX - ipaddress.IPv4Network(pool).prefixlen))


class SubnetAllocator:
    def __init__(self, store: Store, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    def allocate(self, profile: str, cid: str) -> str:
        """Idempotent per (profile, cid): returns the existing /28 or the first free one."""
        pool = POOLS[profile]
        with self.store.tx() as conn:
            row = conn.execute("SELECT idx FROM subnets WHERE profile = ? AND cid = ?", (profile, cid)).fetchone()
            if row is not None:
                return subnet_for(pool, int(row["idx"]))
            used = {int(r["idx"]) for r in conn.execute("SELECT idx FROM subnets WHERE pool = ?", (pool,))}
            idx = next((i for i in range(pool_size(pool) - RESERVED_TAIL) if i not in used), None)
            if idx is None:
                raise PoolExhausted(pool)
            conn.execute(
                "INSERT INTO subnets (profile, idx, cid, allocated_at, pool) VALUES (?, ?, ?, ?, ?)",
                (profile, idx, cid, to_iso(self.clock.now()), pool),
            )
            return subnet_for(pool, idx)

    def release(self, cid: str, profile: str | None = None) -> list[str]:
        """Release the cid's subnets (all, or one profile's); returns the profiles released."""
        with self.store.tx() as conn:
            if profile is None:
                rows = conn.execute("SELECT profile FROM subnets WHERE cid = ?", (cid,)).fetchall()
                conn.execute("DELETE FROM subnets WHERE cid = ?", (cid,))
            else:
                rows = conn.execute(
                    "SELECT profile FROM subnets WHERE cid = ? AND profile = ?", (cid, profile)
                ).fetchall()
                conn.execute("DELETE FROM subnets WHERE cid = ? AND profile = ?", (cid, profile))
        return [str(r["profile"]) for r in rows]

    def allocated(self, cid: str) -> dict[str, str]:
        with self.store.lock:
            rows = self.store.conn.execute("SELECT profile, idx, pool FROM subnets WHERE cid = ?", (cid,)).fetchall()
        return {str(r["profile"]): subnet_for(str(r["pool"]), int(r["idx"])) for r in rows}
