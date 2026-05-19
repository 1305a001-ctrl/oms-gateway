"""Bankroll refresher coroutine.

Runs once per `bankroll_refresh_interval_sec` (default 60s). Reads the
total realized PnL from Postgres `positions` table, computes the active
tier, and persists to Redis via bankroll_aware_sizing.write_bankroll_state.

Main loop integration: import + spawn as a long-running task alongside
the other workers in oms_gateway.main.

Failure handling: per-cycle try/except so a transient Postgres outage
doesn't kill the refresher; we just log and try again next cycle. The
preflight reader falls back to the static cap if the cache is stale.
"""
from __future__ import annotations

import asyncio
import logging
import time

import asyncpg

from oms_gateway import bankroll_aware_sizing as bas
from oms_gateway.settings import settings  # noqa: F401 (used by _excluded_slugs)

log = logging.getLogger(__name__)


# Read total realized PnL across all closed positions that ACTUALLY
# moved wallet capital. Polymarket-only because that's the only venue
# wallet capital actually flows through right now.
#
# Excluded:
#   - `metadata.paper_purged='true'` — left over from a 2026-05-19 cleanup
#     of pre-flip paper rows that accidentally left their PnL intact.
#   - `metadata.paper='true'` — Option B paper-mode positions (2026-05-19+),
#     simulated fills with no on-chain movement.
#   - Strategy slugs in `bankroll_excluded_strategy_slugs_csv` —
#     legacy simulation rows (publisher-taker, politics-momentum) with
#     inflated qty + fake exits that summed to $179k+ of false PnL.
#
# Without these filters, the refresher reports pnl=$216k → tier T6_top
# → budget $5000+ → if any strategy were flipped live, oms-gateway would
# authorize wildly-oversize trades against the real $437 bankroll.
#
# The slug list lives in settings (`bankroll_excluded_strategy_slugs_csv`)
# so the operator can extend it via env if new simulation strategies
# appear, without code change.
TOTAL_REALIZED_PNL_SQL = """
SELECT COALESCE(SUM(p.realized_pnl_usd), 0)::DOUBLE PRECISION AS pnl
FROM positions p
LEFT JOIN strategies s ON s.id = p.strategy_id
WHERE p.status = 'closed'
  AND p.venue = 'polymarket'
  AND COALESCE(p.metadata->>'paper_purged', 'false') != 'true'
  AND COALESCE(p.metadata->>'paper', 'false') != 'true'
  AND (s.slug IS NULL OR NOT (s.slug = ANY($1::text[])))
"""


def _excluded_slugs() -> list[str]:
    """Parse the CSV setting into a list. Empty CSV → empty list (no
    slug exclusion, only paper/paper_purged filters apply)."""
    csv = (settings.bankroll_excluded_strategy_slugs_csv or "").strip()
    return [s.strip() for s in csv.split(",") if s.strip()]


async def fetch_total_realized_pnl(pool: asyncpg.Pool) -> float:
    """Async: total realized PnL across closed Polymarket positions that
    actually moved wallet capital (excludes paper + known simulation slugs)."""
    excluded = _excluded_slugs()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(TOTAL_REALIZED_PNL_SQL, excluded)
    return float(row["pnl"] if row and row["pnl"] is not None else 0.0)


async def run_once(pool: asyncpg.Pool) -> bas.BankrollTier:
    """One refresh cycle. Reads PnL, picks tier, writes state. Returns the tier."""
    pnl = await fetch_total_realized_pnl(pool)
    tier = bas.select_tier(pnl)
    state = bas.to_state_payload(realized_pnl_usd=pnl, tier=tier, now_unix=time.time())
    await bas.write_bankroll_state(state)
    log.info(
        "bankroll.refreshed pnl=%.2f tier=%s budget=$%.0f notional=$%.0f",
        pnl, tier.label, tier.strategy_budget_usd, tier.order_notional_usd,
    )
    return tier


async def run_loop(pool: asyncpg.Pool) -> None:
    """Forever: refresh every `bankroll_refresh_interval_sec`.

    Each cycle is wrapped in try/except so a transient failure doesn't
    kill the loop.
    """
    interval = max(10, int(settings.bankroll_refresh_interval_sec))
    log.info("bankroll.refresher_starting interval=%ds", interval)
    while True:
        try:
            await run_once(pool)
        except Exception:
            log.exception("bankroll.refresh_cycle_failed")
        await asyncio.sleep(interval)


__all__ = [
    "TOTAL_REALIZED_PNL_SQL",
    "fetch_total_realized_pnl",
    "run_once",
    "run_loop",
]
