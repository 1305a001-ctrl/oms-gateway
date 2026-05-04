"""Sizing + side/venue derivation tests."""
import pytest

from oms_gateway.sizing import compute_notional, derive_side, derive_venue


def test_swing_bucket_default_size():
    """swing bucket = 1.5% × $10000 = $150 max, scaled by confidence."""
    notional = compute_notional(
        bucket="swing",
        alpha_metadata={},
        confidence=1.0,
    )
    assert notional == 150.0


def test_fast_intraday_bucket_smaller():
    notional = compute_notional(
        bucket="fast-intraday",
        alpha_metadata={},
        confidence=1.0,
    )
    assert notional == 50.0  # 0.5% × $10k


def test_conviction_bucket_largest():
    notional = compute_notional(
        bucket="conviction",
        alpha_metadata={},
        confidence=1.0,
    )
    assert notional == 500.0  # 5% × $10k


def test_unknown_bucket_falls_back_to_default():
    notional = compute_notional(
        bucket=None,
        alpha_metadata={},
        confidence=1.0,
    )
    # default_per_trade_pct=1.0 → $100
    assert notional == 100.0


def test_alpha_hint_capped_by_bucket():
    """Alpha asks for $999, bucket=fast-intraday cap=$50 → returns 50, not 999."""
    notional = compute_notional(
        bucket="fast-intraday",
        alpha_metadata={"suggested_notional_usd": 999.0},
        confidence=1.0,
    )
    assert notional == 50.0


def test_alpha_hint_below_cap_used():
    """Alpha asks for $40, bucket=fast-intraday cap=$50 → returns 40."""
    notional = compute_notional(
        bucket="fast-intraday",
        alpha_metadata={"suggested_notional_usd": 40.0},
        confidence=1.0,
    )
    assert notional == 40.0


def test_confidence_scales_size():
    """Conf 0.5 should halve the size."""
    notional = compute_notional(
        bucket="swing",
        alpha_metadata={},
        confidence=0.5,
    )
    assert notional == 75.0


def test_confidence_floor_at_quarter():
    """Conf below 0.25 floors at 25% to avoid dust trades."""
    notional = compute_notional(
        bucket="swing",
        alpha_metadata={},
        confidence=0.05,
    )
    assert notional == 37.5  # 150 × 0.25


def test_invalid_alpha_hint_ignored():
    notional = compute_notional(
        bucket="swing",
        alpha_metadata={"suggested_notional_usd": "not a number"},
        confidence=1.0,
    )
    assert notional == 150.0


def test_zero_alpha_hint_ignored():
    notional = compute_notional(
        bucket="swing",
        alpha_metadata={"suggested_notional_usd": 0},
        confidence=1.0,
    )
    assert notional == 150.0  # falls back to bucket cap


def test_derive_side_long():
    assert derive_side("long") == "buy"


def test_derive_side_short():
    assert derive_side("short") == "sell"


def test_derive_side_flat():
    assert derive_side("flat") == "close"


def test_derive_side_watch_raises():
    with pytest.raises(ValueError):
        derive_side("watch")


def test_derive_venue_stocks():
    assert derive_venue("stocks", "AAPL") == "alpaca"


def test_derive_venue_crypto():
    assert derive_venue("crypto", "BTC-USDT") == "okx"


def test_derive_venue_predictions():
    assert derive_venue("predictions", "poly:fed-rate-cut") == "polymarket"


def test_derive_venue_forex():
    assert derive_venue("forex", "EUR/USD") == "oanda"
