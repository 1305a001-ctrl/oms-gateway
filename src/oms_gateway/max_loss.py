"""Max-loss exposure cap for short positions on probability markets.

Background — the 2026-05-09 -$39,496 paper loss on `wta-gauff-sierra-2026-05-09`:
poly-crypto-momentum shorted YES at $0.093, qty 43,547 shares. Entry
notional: ~$4k. Max loss when YES → 1.0: qty × (1 - entry) ≈ $39.5k.
The existing per-position cap checks ENTRY notional (within cap), not
max-loss exposure. The cap let the trade through; the loss was 10x what
the cap intended.

This module provides a pure helper to compute max-loss-USD for short
positions on prediction markets and check it against the configured
per-position cap. Backward-compatible additive — existing per-position
notional cap stays in place; this is a SECOND gate that catches the
short-YES-at-low-price case.

Wire-up (CALLER NOTE for oms_gateway/preflight.py):
    from oms_gateway.max_loss import check_max_loss_for_short_prediction

    # ... after _would_breach_position_cap returns OK ...
    if alpha.asset_class == "predictions" and alpha.direction == "short":
        breach = check_max_loss_for_short_prediction(
            qty=trade_qty, entry_price=trade_price,
            per_position_cap_usd=position_cap_usd,
        )
        if breach is not None:
            return Decision(accept=False, reason=breach)

This wire-up is intentionally NOT in this commit — preflight.py has
un-PR'd Tier 2-4 modifications that should land as one batch.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MaxLossCheck:
    """Result of the max-loss check.

    `breach_reason` is None when the check passes; populated with a
    human-readable string when the trade exceeds the cap.
    """
    max_loss_usd: float
    cap_usd: float
    breach_reason: str | None


def max_loss_for_short_prediction(qty: float, entry_price: float) -> float:
    """Pure: max possible loss when shorting a probability-market YES token.

    Premise: probability tokens settle at $0 or $1. Short YES at $entry
    means we sold qty YES tokens, received `qty * entry` USD. If YES
    settles at $1, we have to buy back at $1 → spend `qty * 1` USD →
    net loss = qty * (1 - entry).

    Returns 0.0 for invalid inputs (qty<=0 or entry<=0 or entry>=1) — the
    caller should not have constructed a short on those.
    """
    if qty <= 0 or entry_price <= 0 or entry_price >= 1.0:
        return 0.0
    return qty * (1.0 - entry_price)


def check_max_loss_for_short_prediction(
    *,
    qty: float,
    entry_price: float,
    per_position_cap_usd: float,
) -> str | None:
    """Pure: returns a breach reason if max-loss exceeds the cap, else None.

    Use the SAME cap value that the existing per-position notional check
    uses — semantics are "max we want to lose on a single position".
    For longs, max-loss = entry notional, so the existing check covers it.
    For shorts on prediction markets, max-loss > entry notional, so this
    second gate is needed.
    """
    if per_position_cap_usd <= 0:
        return None
    max_loss = max_loss_for_short_prediction(qty, entry_price)
    if max_loss > per_position_cap_usd:
        return (
            f"max_loss_breach: short on probability market would lose "
            f"${max_loss:.2f} if YES → 1.0 (qty={qty}, entry={entry_price:.4f}); "
            f"per-position cap is ${per_position_cap_usd:.2f}"
        )
    return None


def check_max_loss(check: MaxLossCheck) -> str | None:
    """Convenience: pass through breach_reason from a pre-built check."""
    return check.breach_reason


__all__ = [
    "MaxLossCheck",
    "check_max_loss",
    "check_max_loss_for_short_prediction",
    "max_loss_for_short_prediction",
]
