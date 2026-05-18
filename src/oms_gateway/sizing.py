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


def _strategy_budget_cap_usd(strategy_slug: str | None) -> float | None:
    """Pure: resolve the per-strategy budget cap (override first, default fallback).

    Mirrors `preflight._would_breach_strategy_budget` lookup so sizing
    pre-caps at the same threshold the preflight check enforces.
    Returns None when no cap is configured (sizing is unconstrained).
    """
    if not strategy_slug:
        return None
    from oms_gateway.preflight import _parse_strategy_caps
    overrides = _parse_strategy_caps(settings.strategy_budget_overrides)
    cap = overrides.get(strategy_slug)
    if cap is None and settings.default_strategy_budget_usd > 0:
        cap = settings.default_strategy_budget_usd
    if cap is None or cap <= 0:
        return None
    return cap


def _strategy_order_cap_usd(strategy_slug: str | None) -> float | None:
    """Pure: per-strategy per-order cap (separate from total-exposure budget).

    Use case: live-mode flips need a per-order ceiling that's independent
    of the strategy's total-exposure budget. Example:
      STRATEGY_ORDER_CAP_OVERRIDES=poly-publisher-taker-long=20
    means each individual order for that slug sizes to ≤ $20 regardless
    of the strategy's budget room or bucket cap. The strategy can still
    accumulate up to `strategy_budget_overrides` total open exposure
    across many $20 orders.

    Returns None when no override is set (sizing is unconstrained by
    this cap; the budget + bucket caps still apply).
    """
    if not strategy_slug:
        return None
    raw = getattr(settings, "strategy_order_cap_overrides", "")
    if not raw:
        return None
    from oms_gateway.preflight import _parse_strategy_caps
    overrides = _parse_strategy_caps(raw)
    cap = overrides.get(strategy_slug)
    if cap is None or cap <= 0:
        return None
    return cap


def compute_notional(
    *,
    bucket: str | None,
    alpha_metadata: dict[str, Any],
    confidence: float,
    asset: str | None = None,
    lp_multiplier: float = 1.0,
    strategy_slug: str | None = None,
    strategy_open_exposure_usd: float = 0.0,
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
        strategy_slug: when set, the result is capped at the per-strategy
                       budget's remaining headroom (cap - existing exposure).
                       Prevents the preflight strategy_budget check from
                       waste-rejecting trades that could have just been sized
                       smaller instead.
        strategy_open_exposure_usd: caller-fetched open exposure for the
                       strategy (from db.strategy_open_exposure_usd). Default
                       0.0 = no existing positions.

    Returns:
        notional in USD, always >= 0. May be 0.0 if the strategy is already
        at its budget cap (caller should treat 0 as "skip this trade").
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

    # Phase 3.2 — cap at remaining per-strategy budget headroom.
    # The preflight strategy_budget check still runs as a defensive
    # backstop; here we ensure sizing doesn't propose what preflight
    # would reject, eliminating the rejection-spam path.
    cap = _strategy_budget_cap_usd(strategy_slug)
    if cap is not None:
        remaining = max(0.0, cap - strategy_open_exposure_usd)
        scaled = min(scaled, remaining)

    # 2026-05-18 — per-strategy ORDER cap (independent of budget).
    # Lets a strategy accumulate up to its budget via many small orders,
    # which the adapter's hard ceiling would otherwise refuse. Critical
    # for live-mode flips where the operator wants e.g. $20/order × 20
    # concurrent positions = $400 total. Without this, oms-gateway sizes
    # to $400 → adapter refuses with `order.refused_ceiling`.
    order_cap = _strategy_order_cap_usd(strategy_slug)
    if order_cap is not None:
        scaled = min(scaled, order_cap)

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
