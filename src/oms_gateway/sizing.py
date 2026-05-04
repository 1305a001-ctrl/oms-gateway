"""Bucket-aware sizing.

Computes the notional USD for an order. Pure function — no I/O.

Priority:
  1. Alpha can hint a size via metadata.suggested_notional_usd.
  2. Bucket cap: bucket_size_pct_max[bucket] * paper_account_equity_usd.
  3. Fallback: default_per_trade_pct * paper_account_equity_usd.

The min of (alpha hint, bucket cap) is taken so a strategy can never exceed
its bucket allowance.
"""
from typing import Any

from oms_gateway.settings import settings


def compute_notional(
    *,
    bucket: str | None,
    alpha_metadata: dict[str, Any],
    confidence: float,
) -> float:
    """Return the order's notional USD.

    Args:
        bucket: 'fast-intraday' / 'swing' / 'conviction' / 'poly-bet' / 'hedge' / None.
        alpha_metadata: Alpha.metadata. May contain 'suggested_notional_usd'.
        confidence: alpha.confidence (0..1). Sub-conviction sizes scale linearly.

    Returns:
        notional in USD, always > 0.
    """
    equity = settings.paper_account_equity_usd

    bucket_cap_pct = settings.bucket_size_pct_max.get(
        bucket or "", settings.default_per_trade_pct
    )
    bucket_cap_usd = (bucket_cap_pct / 100.0) * equity

    alpha_hint = alpha_metadata.get("suggested_notional_usd")
    if isinstance(alpha_hint, int | float) and alpha_hint > 0:
        proposed = min(float(alpha_hint), bucket_cap_usd)
    else:
        proposed = bucket_cap_usd

    # Scale by confidence — a 0.5-conf alpha gets half the bucket size.
    # Floor at 25% of bucket cap so we don't put on dust trades.
    confidence = max(0.0, min(1.0, confidence))
    scaled = proposed * max(0.25, confidence)

    return round(scaled, 2)


def derive_side(direction: str) -> str:
    """Map Alpha.direction → oms_intents.side."""
    if direction == "long":
        return "buy"
    if direction == "short":
        return "sell"
    if direction == "flat":
        return "close"
    # 'watch' alphas should never reach here — caller filters them out.
    raise ValueError(f"unsupported alpha direction: {direction}")


def derive_venue(asset_class: str, asset: str) -> str:
    """Map asset_class → default venue. Phase 2 paper defaults; broker
    selection logic moves into a per-bucket router in Phase 2.5.
    """
    if asset_class == "stocks":
        return "alpaca"
    if asset_class == "crypto":
        # Spot pairs → okx; perps → bybit. asset format hint:
        # 'BTC-USDT', 'ETH-USDT-PERP'. v0.1 defaults all → okx.
        return "okx"
    if asset_class == "predictions":
        return "polymarket"
    if asset_class == "forex":
        return "oanda"
    return "unknown"
