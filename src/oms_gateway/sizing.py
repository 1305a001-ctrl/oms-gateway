"""Bucket-aware sizing.

Computes the notional USD for an order. Pure function — no I/O.

Priority (highest to lowest):
  1. Alpha can hint a size via metadata.suggested_notional_usd.
  2. Per-symbol override: bucket_size_overrides["<ASSET>:<bucket>"]
     (Phase 2.7 — lets ETH momentum size differently from BTC momentum
     even when both share the fast-intraday bucket).
  3. Bucket cap: bucket_size_pct_max[bucket] * paper_account_equity_usd.
  4. Fallback: default_per_trade_pct * paper_account_equity_usd.

The min of (alpha hint, resolved cap) is taken so a strategy can never
exceed its allowance.

Phase 3.0 (2026-05-15) — Liquidity Pulse multiplier:
  After confidence scaling, the result is multiplied by the LP risk
  multiplier (`lp_multiplier` param, defaults to 1.0 = no scaling).
  Callers fetch the multiplier from `lp_multiplier.fetch_multiplier`
  before invoking this function. When LP detects elevated spread
  velocity on the alpha's asset, the multiplier drops below 1.0 and
  this function returns a SMALLER notional — automatic risk-down.
  Unknown / non-LP-tracked assets (stocks, forex, etc.) receive
  lp_multiplier=1.0 → unchanged behavior.
"""
from typing import Any

from oms_gateway.settings import settings


def parse_size_overrides(raw: str) -> dict[str, float]:
    """Pure: parse env-var string → {'<ASSET>:<bucket>': pct}.

    Format:
        "BTC-USDT:fast-intraday=0.7,ETH-USDT:fast-intraday=0.4"
    Keys are uppercased on the asset, lowercased on the bucket.
    Bad / unparseable entries are silently skipped (so a typo on one
    line doesn't kill startup).
    """
    out: dict[str, float] = {}
    if not raw:
        return out
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        if ":" not in key:
            continue
        asset, bucket = key.split(":", 1)
        asset = asset.strip().upper()
        bucket = bucket.strip().lower()
        if not asset or not bucket:
            continue
        try:
            pct = float(value.strip())
        except (ValueError, TypeError):
            continue
        if pct <= 0:
            continue
        out[f"{asset}:{bucket}"] = pct
    return out


def _resolve_bucket_pct(*, asset: str | None, bucket: str | None) -> float:
    """Per-symbol override → bucket cap → default fallback. Pure."""
    overrides = parse_size_overrides(settings.bucket_size_overrides)
    if asset and bucket:
        key = f"{asset.upper()}:{bucket.lower()}"
        if key in overrides:
            return overrides[key]
    return settings.bucket_size_pct_max.get(
        bucket or "", settings.default_per_trade_pct
    )


def compute_notional(
    *,
    bucket: str | None,
    alpha_metadata: dict[str, Any],
    confidence: float,
    asset: str | None = None,
    lp_multiplier: float = 1.0,
) -> float:
    """Return the order's notional USD.

    Args:
        bucket: 'fast-intraday' / 'swing' / 'conviction' / 'poly-bet' / 'hedge' / None.
        alpha_metadata: Alpha.metadata. May contain 'suggested_notional_usd'.
        confidence: alpha.confidence (0..1). Sub-conviction sizes scale linearly.
        asset: Alpha.asset (e.g. 'BTC-USDT', 'NVDA'). Enables per-symbol override.
        lp_multiplier: Liquidity Pulse risk multiplier in [0.5, 2.0].
                       Default 1.0 (no scaling). Callers fetch via
                       `lp_multiplier.fetch_multiplier` before invocation.
                       LP-untracked assets default to 1.0.

    Returns:
        notional in USD, always > 0.
    """
    equity = settings.paper_account_equity_usd

    bucket_cap_pct = _resolve_bucket_pct(asset=asset, bucket=bucket)
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

    # LP multiplier scales AFTER the confidence floor so a real shock can
    # take the order below the 25%-of-bucket dust floor — that's the whole
    # point of dynamic risk filtering. Clamp to [0.5, 2.0] for safety.
    lp = max(0.5, min(2.0, lp_multiplier))
    scaled *= lp

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
        # All crypto (spot + perps) routes through binance — the only crypto
        # adapter live as of v0.2. OKX/Bybit deferred pending account access.
        return "binance"
    if asset_class == "predictions":
        return "polymarket"
    if asset_class == "forex":
        return "oanda"
    return "unknown"
