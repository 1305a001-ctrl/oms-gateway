"""Liquidity Pulse multiplier — internal risk filter for own-strategy sizing.

Reads `liquidity-pulse:<asset>:multiplier` from local Redis (published by
the liquidity-pulse engine on ai-primary) and returns a scalar in
[0.5, 2.0] used to scale each strategy's proposed notional.

Strategy:
  - When LP shows shock or elevated velocity on an asset, multiplier drops
    below 1.0 → we trade SMALLER on that asset.
  - When LP shows calm + favorable conditions, multiplier sits at 1.0.
  - When LP data is stale (>30s) or absent (unknown asset class), we
    default to 1.0 — never amplify on missing data.

Alias resolution:
  alpha.asset is exchange-formatted ('BTC-USDT', 'ETH-USDT', 'BTC',
  'SOLUSDT') — we extract the asset root and lowercase. Only crypto majors
  in our 7-feed Chainlink Data Streams universe are scaled; everything
  else returns 1.0 (no-op).

Pure helpers (alias resolution, parse) tested without infra.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


# Our 7 entitled Chainlink Data Streams feeds — these are the assets the LP
# engine publishes multipliers for. Anything not in this set → 1.0 (no scaling).
LP_TRACKED_ASSETS: frozenset[str] = frozenset(
    {"btc", "eth", "sol", "bnb", "xrp", "doge", "hype"}
)

# How fresh the cached multiplier must be to be trusted, in seconds.
# Beyond this we default to 1.0 (engine may be down).
DEFAULT_FRESHNESS_SEC: float = 30.0

# Hard clamps so a buggy engine output can't blow up sizing.
MIN_MULTIPLIER: float = 0.5
MAX_MULTIPLIER: float = 2.0


def alpha_asset_to_lp_alias(alpha_asset: str | None) -> str | None:
    """Pure: map an alpha's asset string → LP alias, or None if not tracked.

    Examples:
      "BTC-USDT" → "btc"
      "ETH"      → "eth"
      "SOLUSDT"  → "sol"
      "AAPL"     → None      (stock — not in LP universe)
      "EUR/USD"  → None      (forex — not in LP universe)
      None       → None
    """
    if not alpha_asset:
        return None
    # Strip separators + take the longest matching prefix from our tracked set
    normalized = (
        alpha_asset.lower()
        .replace("-", "")
        .replace("/", "")
        .replace("_", "")
    )
    # Prefer the longest match (so "hype" wins over "h" in 'hyperliquid' edge cases)
    for alias in sorted(LP_TRACKED_ASSETS, key=len, reverse=True):
        if normalized.startswith(alias):
            return alias
    return None


def parse_multiplier_payload(
    raw: str | bytes | None,
    *,
    now_unix: float | None = None,
    max_age_sec: float = DEFAULT_FRESHNESS_SEC,
) -> float:
    """Pure: parse a Redis liquidity-pulse:<asset>:multiplier payload → float.

    Returns 1.0 (no-op) when:
      - raw is None / empty
      - JSON parse fails
      - multiplier value missing or non-numeric
      - payload older than max_age_sec
    Otherwise returns the multiplier clamped to [MIN_MULTIPLIER, MAX_MULTIPLIER].
    """
    if not raw:
        return 1.0
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    try:
        payload: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return 1.0

    try:
        multiplier = float(payload.get("multiplier", 1.0))
    except (TypeError, ValueError):
        return 1.0

    # Freshness gate
    computed_at = payload.get("computed_at_unix")
    if computed_at is not None:
        try:
            age = (now_unix if now_unix is not None else time.time()) - float(computed_at)
            if age > max_age_sec:
                return 1.0
        except (TypeError, ValueError):
            pass

    return max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, multiplier))


async def fetch_multiplier(
    redis_client: Any,
    alpha_asset: str | None,
    *,
    max_age_sec: float = DEFAULT_FRESHNESS_SEC,
) -> float:
    """Fetch + parse the multiplier for an alpha. Always returns a float;
    returns 1.0 on any failure path (unknown asset, Redis down, stale, bad JSON).

    Caller passes the existing async Redis handle from oms-gateway's
    redis_client module — no separate connection.
    """
    alias = alpha_asset_to_lp_alias(alpha_asset)
    if alias is None:
        return 1.0
    try:
        raw = await redis_client.get(f"liquidity-pulse:{alias}:multiplier")
    except Exception:
        log.warning("lp_multiplier.fetch_failed alias=%s — defaulting to 1.0", alias)
        return 1.0
    return parse_multiplier_payload(raw, max_age_sec=max_age_sec)


__all__ = [
    "DEFAULT_FRESHNESS_SEC",
    "LP_TRACKED_ASSETS",
    "MAX_MULTIPLIER",
    "MIN_MULTIPLIER",
    "alpha_asset_to_lp_alias",
    "fetch_multiplier",
    "parse_multiplier_payload",
]
