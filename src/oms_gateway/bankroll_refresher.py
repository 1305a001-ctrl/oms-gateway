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
from oms_gateway.settings import settings

log = logging.getLogger(__name__)


# Read total realized PnL across all closed positions.
# Polymarket-only by default so the live-flip ramp is driven by the
# already-validated lane, not by Binance/Alpaca paper noise. Operator
# can broaden via env (TODO if needed).
TOTAL_REALIZED_PNL_SQL = """
SELECT COALESCE(SUM(realized_pnl_usd), 0)::DOUBLE PRECISION AS pnl
FROM positions
WHERE status = 'closed'
  AND venue = 'polymarket'
"""


async def fetch_total_realized_pnl(pool: asyncpg.Pool) -> float:
    """Async: total realized PnL across closed Polymarket positions."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(TOTAL_REALIZED_PNL_SQL)
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
