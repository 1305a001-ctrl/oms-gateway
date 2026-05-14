"""Tests for the LP multiplier scaling in compute_notional."""
from __future__ import annotations

from oms_gateway.sizing import compute_notional


def test_lp_multiplier_one_is_noop() -> None:
    """LP multiplier = 1.0 leaves the notional unchanged vs no-LP-param."""
    base = compute_notional(
        bucket="swing", alpha_metadata={}, confidence=1.0,
    )
    with_lp = compute_notional(
        bucket="swing", alpha_metadata={}, confidence=1.0, lp_multiplier=1.0,
    )
    assert base == with_lp


def test_lp_multiplier_scales_down() -> None:
    """LP multiplier = 0.80 → notional is 80% of the no-scaling case."""
    base = compute_notional(
        bucket="swing", alpha_metadata={}, confidence=1.0,
    )
    scaled = compute_notional(
        bucket="swing", alpha_metadata={}, confidence=1.0, lp_multiplier=0.80,
    )
    assert abs(scaled - base * 0.80) < 0.02   # within rounding


def test_lp_multiplier_below_min_clamped() -> None:
    """Values below 0.5 are clamped to 0.5 (safety)."""
    safe = compute_notional(
        bucket="swing", alpha_metadata={}, confidence=1.0, lp_multiplier=0.5,
    )
    too_low = compute_notional(
        bucket="swing", alpha_metadata={}, confidence=1.0, lp_multiplier=0.1,
    )
    assert safe == too_low


def test_lp_multiplier_above_max_clamped() -> None:
    """Values above 2.0 are clamped to 2.0."""
    cap = compute_notional(
        bucket="swing", alpha_metadata={}, confidence=1.0, lp_multiplier=2.0,
    )
    too_high = compute_notional(
        bucket="swing", alpha_metadata={}, confidence=1.0, lp_multiplier=10.0,
    )
    assert cap == too_high


def test_lp_multiplier_can_take_below_dust_floor() -> None:
    """When LP signals shock, multiplier should bypass the 25% confidence floor.

    Base swing bucket = $150. At LP=0.5, notional = $150 × 0.5 = $75 ≈ 50% of
    bucket. That's BELOW the 25%-of-bucket dust floor (which exists for
    sub-conviction alphas) — but LP scaling intentionally overrides because
    the WHOLE point of the multiplier is to risk-down during shocks.
    """
    base = compute_notional(
        bucket="swing", alpha_metadata={}, confidence=1.0,
    )
    shock = compute_notional(
        bucket="swing", alpha_metadata={}, confidence=1.0, lp_multiplier=0.5,
    )
    assert shock < base * 0.6   # well below
    # And it should still be > 0 (not zero'd out — we still trade, just smaller)
    assert shock > 0


def test_lp_multiplier_composes_with_confidence() -> None:
    """Both confidence and LP multiplier apply; final = base × max(0.25, conf) × lp."""
    # bucket=swing → cap $150. confidence=0.5 → max(0.25, 0.5)=0.5 → $75. LP=0.8 → $60.
    notional = compute_notional(
        bucket="swing", alpha_metadata={}, confidence=0.5, lp_multiplier=0.8,
    )
    assert abs(notional - 60.0) < 0.02
