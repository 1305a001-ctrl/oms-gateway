"""Bankroll-aware trade sizing — scales caps with realized PnL.

Goal: as the trading book makes money, the per-strategy budget cap and
the per-order notional cap both ramp up automatically, without operator
intervention. As the book draws down, the caps ratchet back.

Sizing model: tier ladder
─────────────────────────
A monotonic ladder of (realized_pnl_threshold, strategy_budget, order_notional)
triples. The active tier is the highest one whose threshold ≤ current
realized PnL across the book. Per-tier caps OVERRIDE the static settings
when bankroll_aware_sizing_enabled=True.

Bankroll definition
───────────────────
We define "bankroll" = initial_capital_usd + sum(realized_pnl_usd across
all closed positions). Unrealized PnL is intentionally EXCLUDED because:
  (a) Open positions can swing; basing live sizing on mark-to-market
      causes margin spirals
  (b) Realized = what's actually in the wallet; ratcheting from
      realized is conservative + auditable

Floor + ceiling
───────────────
- Floor: cap can never go BELOW tier-0 (currently $200 / $50). Prevents
  a single bad day from collapsing sizing to noise.
- Ceiling: cap can never go ABOVE the top tier. Hard limit at
  $5000 / $1200 until operator explicitly extends the ladder.

Refresh cadence
───────────────
A background task in main.py XADDs current bankroll + tier to Redis
every 60s. Preflight reads from Redis (cheap lookup) on each intent
evaluation. Stale data: if the refresher dies, the cached value stays
until cleared — operator should monitor the refresh_at_unix freshness.

Async/IO is in `update_bankroll_state()`; everything else is pure.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from oms_gateway.redis_client import r
from oms_gateway.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BankrollTier:
    """One rung of the sizing ladder. Pure data class."""
    pnl_threshold_usd: float
    strategy_budget_usd: float
    order_notional_usd: float
    label: str  # for log/UI


# Default ladder — designed for $500 starting capital, conservative ramp.
# Operator can override the whole ladder via env (TODO: ladder serializer).
# Each tier doubles trade size after ~3-5x the prior tier's profit budget.
DEFAULT_LADDER: tuple[BankrollTier, ...] = (
    BankrollTier(    0.0,   200.0,    50.0, "T0_seed"),       # $500 start
    BankrollTier(  500.0,   400.0,   100.0, "T1_first_profit"),  # +$500 realized
    BankrollTier( 1500.0,   600.0,   150.0, "T2_proven"),      # +$1500
    BankrollTier( 3000.0,   900.0,   225.0, "T3_compound"),    # +$3000
    BankrollTier( 6000.0,  1500.0,   400.0, "T4_scale"),       # +$6000
    BankrollTier(12000.0,  2500.0,   650.0, "T5_mid"),         # +$12k
    BankrollTier(25000.0,  5000.0,  1200.0, "T6_top"),         # +$25k
)


# Redis key — single key, JSON value with tier label + caps + bankroll
BANKROLL_STATE_KEY = "oms:bankroll:state"


# How long the cached state stays valid before preflight ignores it.
# If the refresher dies, after this window we fall back to the static
# settings cap (fail-CLOSED to baseline, not to a stale-high cap).
BANKROLL_STATE_TTL_SEC = 600


def select_tier(
    realized_pnl_usd: float,
    *,
    ladder: tuple[BankrollTier, ...] = DEFAULT_LADDER,
) -> BankrollTier:
    """Pure: pick the highest tier whose threshold ≤ realized_pnl_usd.

    Always returns at least the seed tier (T0). Ladder must be
    sorted ascending by pnl_threshold (we don't sort here — invariant).
    """
    active = ladder[0]
    for tier in ladder:
        if realized_pnl_usd >= tier.pnl_threshold_usd:
            active = tier
        else:
            break
    return active


def to_state_payload(
    *,
    realized_pnl_usd: float,
    tier: BankrollTier,
    now_unix: float | None = None,
) -> dict[str, float | str]:
    """Pure: build the dict we XADD/SET on Redis."""
    return {
        "realized_pnl_usd": realized_pnl_usd,
        "tier_label": tier.label,
        "strategy_budget_usd": tier.strategy_budget_usd,
        "order_notional_usd": tier.order_notional_usd,
        "refresh_at_unix": float(now_unix if now_unix is not None else time.time()),
    }


def is_state_fresh(state: dict, *, max_age_sec: int = BANKROLL_STATE_TTL_SEC) -> bool:
    """Pure: True if the cached state is recent enough to trust."""
    try:
        ts = float(state.get("refresh_at_unix") or 0.0)
    except (TypeError, ValueError):
        return False
    return (time.time() - ts) <= max_age_sec


async def read_bankroll_state() -> dict | None:
    """Async: fetch the cached bankroll state from Redis. None on any error."""
    try:
        raw = await r().get(BANKROLL_STATE_KEY)
    except Exception as e:
        log.debug("bankroll.read_failed err=%s", e)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def write_bankroll_state(state: dict) -> None:
    """Async: persist bankroll state to Redis with TTL safety guard."""
    try:
        await r().set(
            BANKROLL_STATE_KEY,
            json.dumps(state),
            ex=2 * BANKROLL_STATE_TTL_SEC,  # 2× TTL so it can go stale before expiring
        )
    except Exception as e:
        log.warning("bankroll.write_failed err=%s", e)


def effective_strategy_budget(state: dict | None) -> float | None:
    """Pure: tier-derived strategy budget, or None if disabled/stale.

    Returns None when:
      - bankroll-aware sizing disabled in settings
      - cached state missing or stale (>BANKROLL_STATE_TTL_SEC old)
    Caller (preflight) falls back to settings.default_strategy_budget_usd
    when this returns None.
    """
    if not settings.bankroll_aware_sizing_enabled:
        return None
    if state is None or not is_state_fresh(state):
        return None
    try:
        return float(state["strategy_budget_usd"])
    except (TypeError, ValueError, KeyError):
        return None


def effective_order_notional(state: dict | None) -> float | None:
    """Pure: tier-derived max single-order notional, or None if disabled/stale."""
    if not settings.bankroll_aware_sizing_enabled:
        return None
    if state is None or not is_state_fresh(state):
        return None
    try:
        return float(state["order_notional_usd"])
    except (TypeError, ValueError, KeyError):
        return None


__all__ = [
    "BankrollTier",
    "DEFAULT_LADDER",
    "BANKROLL_STATE_KEY",
    "BANKROLL_STATE_TTL_SEC",
    "select_tier",
    "to_state_payload",
    "is_state_fresh",
    "read_bankroll_state",
    "write_bankroll_state",
    "effective_strategy_budget",
    "effective_order_notional",
]
