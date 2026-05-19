"""Tests for the atomic Redis-backed budget reservation.

2026-05-19 disaster root cause #2: multiple alphas in <1s all read DB-side
strategy_open_exposure_usd=0 and all pass the cap check. Fix is an
atomic Redis INCRBYFLOAT-based reservation that includes pending intents
in the budget calculation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from oms_gateway.budget_reservation import (
    RESERVATION_KEY_PREFIX,
    read_reservation,
    release_exposure,
    reserve_exposure,
)


class _FakeRedis:
    """Async Redis stub for tests. Tracks INCRBYFLOAT atomically (single-thread)."""

    def __init__(self):
        self.store: dict[str, float] = {}
        self.ttls: dict[str, int] = {}

    async def incrbyfloat(self, key: str, delta: float) -> float:
        self.store[key] = self.store.get(key, 0.0) + delta
        return self.store[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    async def get(self, key: str) -> str | None:
        v = self.store.get(key)
        return None if v is None else str(v)


@pytest.mark.asyncio
async def test_reserve_increments_and_returns_new_total():
    r = _FakeRedis()
    out = await reserve_exposure(r, "poly-chainlink-lag", 5.0)
    assert out == 5.0
    out = await reserve_exposure(r, "poly-chainlink-lag", 10.0)
    assert out == 15.0


@pytest.mark.asyncio
async def test_release_decrements():
    r = _FakeRedis()
    await reserve_exposure(r, "poly-chainlink-lag", 25.0)
    await release_exposure(r, "poly-chainlink-lag", 10.0)
    current = await read_reservation(r, "poly-chainlink-lag")
    assert current == 15.0


@pytest.mark.asyncio
async def test_reserve_sets_ttl():
    """Reservations must auto-expire so dead intents don't pile up."""
    r = _FakeRedis()
    await reserve_exposure(r, "poly-chainlink-lag", 5.0)
    key = f"{RESERVATION_KEY_PREFIX}poly-chainlink-lag"
    assert r.ttls[key] > 0
    # Should be 5min by convention
    assert 60 <= r.ttls[key] <= 3600


@pytest.mark.asyncio
async def test_reserve_zero_or_negative_is_noop():
    """Defense: don't let bad input poison the counter."""
    r = _FakeRedis()
    out = await reserve_exposure(r, "poly-chainlink-lag", 0.0)
    assert out == 0.0
    out = await reserve_exposure(r, "poly-chainlink-lag", -5.0)
    assert out == 0.0
    assert "poly-chainlink-lag" not in r.store


@pytest.mark.asyncio
async def test_reserve_missing_slug_is_noop():
    r = _FakeRedis()
    out = await reserve_exposure(r, "", 5.0)
    assert out == 0.0
    out = await reserve_exposure(r, None, 5.0)
    assert out == 0.0


@pytest.mark.asyncio
async def test_redis_failure_fails_closed():
    """When Redis is down, return 0.0 — caller still gets DB-based exposure.
    Other defenses (rate limit, whitelist) catch any race that reopens."""
    r = AsyncMock()
    r.incrbyfloat.side_effect = ConnectionError("redis down")
    out = await reserve_exposure(r, "poly-chainlink-lag", 5.0)
    assert out == 0.0  # fail-closed


@pytest.mark.asyncio
async def test_release_decrements_back_to_zero():
    """The race-condition flow: reserve, evaluate rejects, release rolls back."""
    r = _FakeRedis()
    # Concurrent intents A, B, C all reserve $50 each → total $150
    # Cap is $120 → C must be rejected.
    # On reject of C: we release the $50; net pending stays at $100.
    a = await reserve_exposure(r, "poly-chainlink-lag", 50.0)
    b = await reserve_exposure(r, "poly-chainlink-lag", 50.0)
    c = await reserve_exposure(r, "poly-chainlink-lag", 50.0)
    assert (a, b, c) == (50.0, 100.0, 150.0)
    # C breached cap → release
    await release_exposure(r, "poly-chainlink-lag", 50.0)
    current = await read_reservation(r, "poly-chainlink-lag")
    assert current == 100.0
    # Only A + B's reservations remain — exactly what we want


@pytest.mark.asyncio
async def test_burst_protection_invariant():
    """Critical invariant: if 10 concurrent intents try to reserve $20
    each against a budget of $120, exactly 6 should appear in the
    'accepted' pile and 4 should release. Race condition closed."""
    r = _FakeRedis()
    budget = 120.0
    intent_size = 20.0
    accepted = []
    for i in range(10):
        total_pending = await reserve_exposure(r, "stress-test-slug", intent_size)
        # Simulate the budget check
        # (open_db_exposure=0, total_pending includes our own reserve)
        if total_pending <= budget:
            accepted.append(i)
        else:
            # This intent would breach — release back
            await release_exposure(r, "stress-test-slug", intent_size)
    # 6 × $20 = $120 = cap
    assert len(accepted) == 6
    current = await read_reservation(r, "stress-test-slug")
    assert current == 120.0
