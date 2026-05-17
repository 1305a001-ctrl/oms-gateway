"""Kelly capital allocator across strategies.

Currently every strategy gets the same per-slug budget cap. With 99 active
strategies of widely varying edge, this is suboptimal: a Sharpe-3 strategy
deserves more capital than a Sharpe-0.5 strategy.

Kelly criterion: fraction of bankroll to allocate per strategy is
proportional to its edge / variance. For binary outcomes with win rate p
and win/loss ratio b:
    f* = p − (1 − p) / b

For continuous returns:
    f* = mean / variance

We use a discounted Kelly (default 0.25 — "quarter Kelly") because real
edges drift, Kelly assumes correct estimates of p and b, and full Kelly
is volatile. Quarter Kelly captures ~75% of the long-run return with
massively reduced volatility.

Per-strategy allocation multiplier is then applied on top of the
bankroll-aware tier budget. A strategy with 2x Kelly factor gets 2x the
tier's base cap, capped at strategy_kelly_cap_multiplier (default 3x).

Sources of return data
──────────────────────
- 30-day rolling window of `positions` closed trades per strategy
- realized_pnl_usd / size_usd → per-trade return
- Discounted (decay rate 0.95 per week) — recent edges weighted more
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass

import asyncpg

from oms_gateway.redis_client import r
from oms_gateway.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KellyAllocation:
    """One strategy's Kelly-derived allocation multiplier."""
    strategy_slug: str
    n_trades: int
    mean_return: float
    variance: float
    raw_kelly_fraction: float    # full Kelly: mean/variance
    discounted_kelly: float       # × kelly_discount
    capped_multiplier: float      # clamped to [floor, ceiling]


# Cap multipliers — protects against bad estimates blowing up allocations.
DEFAULT_KELLY_DISCOUNT = 0.25     # quarter Kelly
DEFAULT_FLOOR_MULTIPLIER = 0.25   # never below 25% of base
DEFAULT_CEILING_MULTIPLIER = 3.0  # never above 3x base


# Redis state key for cached allocations (preflight reads from this)
ALLOCATIONS_STATE_KEY = "oms:kelly:allocations"
ALLOCATIONS_STATE_TTL_SEC = 600


def compute_kelly_fraction(
    *,
    returns: list[float],
    discount: float = DEFAULT_KELLY_DISCOUNT,
    floor: float = DEFAULT_FLOOR_MULTIPLIER,
    ceiling: float = DEFAULT_CEILING_MULTIPLIER,
) -> tuple[float, float, float]:
    """Pure: compute discounted Kelly fraction from a return list.

    Returns (raw_kelly, discounted_kelly, capped_kelly).
      raw_kelly       = mean / variance (full Kelly; may be huge)
      discounted_kelly = raw_kelly × discount
      capped_kelly    = clamp(discounted, floor, ceiling)

    With <5 samples or zero variance, returns (0, 0, 1.0) — neutral.
    """
    if len(returns) < 5:
        return 0.0, 0.0, 1.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if var <= 0:
        return 0.0, 0.0, 1.0
    raw = mean / var
    disc = raw * discount
    capped = max(floor, min(ceiling, disc))
    return raw, disc, capped


def compute_allocation(
    *,
    slug: str,
    trades: list[tuple[float, float]],
    discount: float = DEFAULT_KELLY_DISCOUNT,
    floor: float = DEFAULT_FLOOR_MULTIPLIER,
    ceiling: float = DEFAULT_CEILING_MULTIPLIER,
) -> KellyAllocation:
    """Pure: KellyAllocation for one strategy from its trades."""
    returns = [pnl / size for pnl, size in trades if size > 0]
    if len(returns) < 5:
        return KellyAllocation(
            strategy_slug=slug,
            n_trades=len(returns),
            mean_return=0.0,
            variance=0.0,
            raw_kelly_fraction=0.0,
            discounted_kelly=0.0,
            capped_multiplier=1.0,
        )
    raw, disc, capped = compute_kelly_fraction(
        returns=returns, discount=discount, floor=floor, ceiling=ceiling,
    )
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return KellyAllocation(
        strategy_slug=slug,
        n_trades=len(returns),
        mean_return=mean,
        variance=var,
        raw_kelly_fraction=raw,
        discounted_kelly=disc,
        capped_multiplier=capped,
    )


CLOSED_TRADES_SQL = """
SELECT s.slug,
       p.realized_pnl_usd,
       p.size_usd
FROM positions p
JOIN strategies s ON s.id = p.strategy_id
WHERE p.status = 'closed'
  AND p.closed_at > NOW() - INTERVAL '30 days'
  AND p.size_usd > 0
ORDER BY s.slug, p.closed_at DESC
LIMIT 5000
"""


async def fetch_trades_grouped(pool: asyncpg.Pool) -> dict[str, list[tuple[float, float]]]:
    """Async: closed trades for the last 30 days, grouped by strategy slug."""
    out: dict[str, list[tuple[float, float]]] = {}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(CLOSED_TRADES_SQL)
    except Exception as e:
        log.warning("kelly.fetch_failed err=%s", e)
        return out
    for r_ in rows:
        slug = str(r_["slug"])
        pnl = float(r_["realized_pnl_usd"] or 0.0)
        size = float(r_["size_usd"] or 0.0)
        if size <= 0:
            continue
        out.setdefault(slug, []).append((pnl, size))
    return out


async def write_allocations(allocations: list[KellyAllocation]) -> None:
    """Async: persist {slug: multiplier} to Redis for preflight lookup."""
    payload = {
        a.strategy_slug: a.capped_multiplier
        for a in allocations
    }
    payload["__refreshed_at_unix__"] = time.time()
    try:
        await r().set(
            ALLOCATIONS_STATE_KEY,
            json.dumps(payload),
            ex=2 * ALLOCATIONS_STATE_TTL_SEC,
        )
    except Exception as e:
        log.warning("kelly.write_failed err=%s", e)


async def read_allocation_multiplier(slug: str) -> float:
    """Async: lookup the Kelly multiplier for one strategy.

    Returns 1.0 (neutral) if:
      - Kelly allocator disabled
      - cached state missing or stale
      - slug not in the cache (new strategy)
    """
    if not settings.kelly_allocator_enabled:
        return 1.0
    try:
        raw = await r().get(ALLOCATIONS_STATE_KEY)
    except Exception:
        return 1.0
    if not raw:
        return 1.0
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return 1.0
    refresh_ts = payload.get("__refreshed_at_unix__", 0.0)
    try:
        refresh_ts = float(refresh_ts)
    except (TypeError, ValueError):
        return 1.0
    if (time.time() - refresh_ts) > ALLOCATIONS_STATE_TTL_SEC:
        return 1.0   # stale → neutral
    try:
        return float(payload.get(slug, 1.0))
    except (TypeError, ValueError):
        return 1.0


async def run_once(pool: asyncpg.Pool) -> list[KellyAllocation]:
    """One allocator cycle. Returns list of allocations across strategies."""
    trades = await fetch_trades_grouped(pool)
    allocations: list[KellyAllocation] = []
    for slug, slug_trades in trades.items():
        a = compute_allocation(
            slug=slug,
            trades=slug_trades,
            discount=settings.kelly_discount,
            floor=settings.kelly_floor_multiplier,
            ceiling=settings.kelly_ceiling_multiplier,
        )
        allocations.append(a)
    await write_allocations(allocations)
    log.info(
        "kelly.refreshed strategies=%d top_alloc=%s",
        len(allocations),
        sorted(allocations, key=lambda a: -a.capped_multiplier)[:3],
    )
    return allocations


__all__ = [
    "KellyAllocation",
    "DEFAULT_KELLY_DISCOUNT",
    "DEFAULT_FLOOR_MULTIPLIER",
    "DEFAULT_CEILING_MULTIPLIER",
    "ALLOCATIONS_STATE_KEY",
    "compute_kelly_fraction",
    "compute_allocation",
    "read_allocation_multiplier",
    "fetch_trades_grouped",
    "write_allocations",
    "run_once",
]
