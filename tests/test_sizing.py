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
    assert derive_venue("crypto", "BTC-USDT") == "binance"


def test_derive_venue_predictions():
    assert derive_venue("predictions", "poly:fed-rate-cut") == "polymarket"


def test_derive_venue_forex():
    assert derive_venue("forex", "EUR/USD") == "oanda"


# ─── Phase 3.2 — per-strategy budget cap on sizing ─────────────────


def test_strategy_slug_unset_no_cap_applied():
    """Without a strategy_slug, sizing is uncapped (back-compat)."""
    notional = compute_notional(
        bucket="conviction",
        alpha_metadata={},
        confidence=1.0,
    )
    assert notional == 500.0  # 5% × $10k unchanged


def test_strategy_default_budget_caps_sizing(monkeypatch):
    """default_strategy_budget_usd=200 caps a $500-notional conviction trade
    when caller provides a strategy_slug and there is no existing exposure."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "default_strategy_budget_usd", 200.0)
    monkeypatch.setattr(settings, "strategy_budget_overrides", "")
    notional = compute_notional(
        bucket="conviction",
        alpha_metadata={},
        confidence=1.0,
        strategy_slug="some-strategy",
        strategy_open_exposure_usd=0.0,
    )
    assert notional == 200.0


def test_strategy_override_caps_sizing(monkeypatch):
    """Per-strategy override beats the default."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "default_strategy_budget_usd", 200.0)
    monkeypatch.setattr(
        settings, "strategy_budget_overrides",
        "some-strategy=350,other-strategy=900",
    )
    notional = compute_notional(
        bucket="conviction",
        alpha_metadata={},
        confidence=1.0,
        strategy_slug="some-strategy",
        strategy_open_exposure_usd=0.0,
    )
    assert notional == 350.0


def test_strategy_existing_exposure_reduces_remaining(monkeypatch):
    """Open positions consume the budget — remaining headroom shrinks."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "default_strategy_budget_usd", 200.0)
    monkeypatch.setattr(settings, "strategy_budget_overrides", "")
    notional = compute_notional(
        bucket="conviction",
        alpha_metadata={},
        confidence=1.0,
        strategy_slug="some-strategy",
        strategy_open_exposure_usd=120.0,
    )
    assert notional == 80.0  # 200 cap - 120 existing = 80 remaining


def test_strategy_fully_consumed_budget_returns_zero(monkeypatch):
    """When existing exposure >= cap, sizing returns 0 (caller skips)."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "default_strategy_budget_usd", 200.0)
    monkeypatch.setattr(settings, "strategy_budget_overrides", "")
    notional = compute_notional(
        bucket="conviction",
        alpha_metadata={},
        confidence=1.0,
        strategy_slug="some-strategy",
        strategy_open_exposure_usd=250.0,
    )
    assert notional == 0.0


def test_strategy_cap_above_bucket_scaled_size_no_op(monkeypatch):
    """If the budget cap exceeds bucket-scaled size, no cap applied."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "default_strategy_budget_usd", 10_000.0)
    monkeypatch.setattr(settings, "strategy_budget_overrides", "")
    notional = compute_notional(
        bucket="conviction",
        alpha_metadata={},
        confidence=1.0,
        strategy_slug="some-strategy",
        strategy_open_exposure_usd=0.0,
    )
    assert notional == 500.0  # bucket size unchanged, well below $10k cap


def test_strategy_zero_budget_skips_cap(monkeypatch):
    """default_strategy_budget_usd=0 → cap check disabled."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "default_strategy_budget_usd", 0.0)
    monkeypatch.setattr(settings, "strategy_budget_overrides", "")
    notional = compute_notional(
        bucket="conviction",
        alpha_metadata={},
        confidence=1.0,
        strategy_slug="some-strategy",
        strategy_open_exposure_usd=0.0,
    )
    assert notional == 500.0  # cap not enforced
