"""Redis-backed atomic budget reservation for per-strategy preflight.

Why this exists (2026-05-19 disaster root cause #2):
  When N alphas arrive within <1s for the same strategy, the preflight
  budget check reads `strategy_open_exposure_usd` from Postgres. The DB
  isn't updated until the position settles (often 5-15s later). So all
  N concurrent reads see $0 open, all pass the cap check, all fire —
  the race that let 9 orders / $90 through before any single $10-cap
  could engage.

The fix: between DB read and preflight decision, atomically INCRBYFLOAT
a Redis key tracking "pending exposure not yet in DB". Include that in
the budget check. On reject: decrement back. On accept: leave it; TTL
auto-expires faster than the position settles, but the DB will have
absorbed it by then, so total exposure remains accurate.

Failure modes:
  - Redis down → fail-CLOSED (reservation reads as 0, race re-opens).
    Acceptable: very rare; in that case rate limit (6s) and whitelist
    still catch the cascade. This module is the THIRD line of defense,
    not the only one.
  - Reservation key gets stuck (process crashed mid-flow) → TTL clears
    after 300s. Worst case: 5min of conservative budget enforcement.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Redis key per strategy. INCRBYFLOAT atomically increments; we DECR on
# reject. TTL ensures dead reservations don't pile up.
RESERVATION_KEY_PREFIX = "oms:strategy_pending_reservation:"
RESERVATION_TTL_SECONDS = 300  # 5 min — longer than typical settlement


def _reservation_key(strategy_slug: str) -> str:
    return f"{RESERVATION_KEY_PREFIX}{strategy_slug}"


async def reserve_exposure(
    redis_client,
    strategy_slug: str,
    proposed_notional_usd: float,
) -> float:
    """Atomically increment the strategy's pending reservation by the
    proposed notional. Returns the NEW total pending value (post-increment).

    Returns 0.0 on Redis failure (fail-CLOSED — race could reopen, but
    other defenses catch it).
    """
    if not strategy_slug or proposed_notional_usd <= 0:
        return 0.0
    try:
        key = _reservation_key(strategy_slug)
        new_val = await redis_client.incrbyfloat(key, proposed_notional_usd)
        # Set/refresh TTL — INCRBYFLOAT doesn't touch TTL on existing key
        await redis_client.expire(key, RESERVATION_TTL_SECONDS)
        return float(new_val)
    except Exception as exc:
        log.warning(
            "budget_reservation.incr_failed slug=%s err=%s",
            strategy_slug, exc,
        )
        return 0.0


async def release_exposure(
    redis_client,
    strategy_slug: str,
    notional_usd: float,
) -> None:
    """Atomically decrement the strategy's pending reservation by the
    given notional. Used on reject to roll back a reservation that
    didn't lead to an order.
    """
    if not strategy_slug or notional_usd <= 0:
        return
    try:
        key = _reservation_key(strategy_slug)
        await redis_client.incrbyfloat(key, -notional_usd)
    except Exception as exc:
        log.warning(
            "budget_reservation.decr_failed slug=%s err=%s",
            strategy_slug, exc,
        )


async def read_reservation(
    redis_client,
    strategy_slug: str,
) -> float:
    """Read current pending reservation (without modifying).

    Returns 0.0 on Redis failure or missing key.
    """
    if not strategy_slug:
        return 0.0
    try:
        raw = await redis_client.get(_reservation_key(strategy_slug))
        return float(raw) if raw else 0.0
    except Exception as exc:
        log.warning(
            "budget_reservation.read_failed slug=%s err=%s",
            strategy_slug, exc,
        )
        return 0.0


__all__ = [
    "RESERVATION_KEY_PREFIX",
    "RESERVATION_TTL_SECONDS",
    "read_reservation",
    "release_exposure",
    "reserve_exposure",
]
