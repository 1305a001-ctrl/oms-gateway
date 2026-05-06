"""Per-symbol sizing override tests (Phase 2.7)."""
from unittest.mock import patch

from oms_gateway.sizing import (
    _resolve_bucket_pct,
    compute_notional,
    parse_size_overrides,
)

# --- parse_size_overrides ---------------------------------------------------

def test_parse_empty():
    assert parse_size_overrides("") == {}


def test_parse_single():
    assert parse_size_overrides("BTC-USDT:fast-intraday=0.7") == {
        "BTC-USDT:fast-intraday": 0.7
    }


def test_parse_multiple():
    out = parse_size_overrides(
        "BTC-USDT:fast-intraday=0.7,ETH-USDT:fast-intraday=0.4,NVDA:swing=2.0"
    )
    assert out == {
        "BTC-USDT:fast-intraday": 0.7,
        "ETH-USDT:fast-intraday": 0.4,
        "NVDA:swing": 2.0,
    }


def test_parse_strips_whitespace():
    out = parse_size_overrides("  BTC-USDT : fast-intraday = 0.7 , ETH-USDT:fast-intraday=0.4 ")
    assert out == {
        "BTC-USDT:fast-intraday": 0.7,
        "ETH-USDT:fast-intraday": 0.4,
    }


def test_parse_uppercases_asset_lowercases_bucket():
    out = parse_size_overrides("btc-usdt:Fast-Intraday=0.7")
    assert out == {"BTC-USDT:fast-intraday": 0.7}


def test_parse_skips_malformed_entries():
    """Bad entries don't kill good ones."""
    out = parse_size_overrides(
        "BTC-USDT:fast-intraday=0.7,INVALID,ETH-USDT:swing=,NOPE=1.0,NVDA:swing=2.0"
    )
    assert out == {
        "BTC-USDT:fast-intraday": 0.7,
        "NVDA:swing": 2.0,
    }


def test_parse_skips_zero_or_negative():
    out = parse_size_overrides("BTC:swing=0,ETH:swing=-1,NVDA:swing=2.0")
    assert out == {"NVDA:swing": 2.0}


def test_parse_skips_non_numeric():
    out = parse_size_overrides("BTC:swing=banana,NVDA:swing=2.0")
    assert out == {"NVDA:swing": 2.0}


# --- _resolve_bucket_pct + compute_notional with overrides -----------------

def _patch(overrides: str):
    return patch("oms_gateway.sizing.settings.bucket_size_overrides", overrides)


def test_override_wins_over_bucket_default():
    """ETH-USDT overrides fast-intraday from 0.5% default to 0.4%."""
    with _patch("ETH-USDT:fast-intraday=0.4"):
        pct = _resolve_bucket_pct(asset="ETH-USDT", bucket="fast-intraday")
    assert pct == 0.4


def test_no_override_falls_back_to_bucket_default():
    with _patch("ETH-USDT:fast-intraday=0.4"):
        # BTC-USDT not in overrides → bucket default 0.5%
        pct = _resolve_bucket_pct(asset="BTC-USDT", bucket="fast-intraday")
    assert pct == 0.5


def test_override_case_insensitive_asset():
    with _patch("BTC-USDT:fast-intraday=0.7"):
        # Caller passes lowercase — should still match.
        pct = _resolve_bucket_pct(asset="btc-usdt", bucket="fast-intraday")
    assert pct == 0.7


def test_override_in_compute_notional_end_to_end():
    """0.7% on $10k = $70 max, scaled at confidence=1.0."""
    with _patch("BTC-USDT:fast-intraday=0.7"):
        notional = compute_notional(
            bucket="fast-intraday",
            alpha_metadata={},
            confidence=1.0,
            asset="BTC-USDT",
        )
    assert notional == 70.0


def test_no_override_keeps_bucket_default_in_compute_notional():
    """ETH not overridden → fast-intraday 0.5% → $50."""
    with _patch("BTC-USDT:fast-intraday=0.7"):
        notional = compute_notional(
            bucket="fast-intraday",
            alpha_metadata={},
            confidence=1.0,
            asset="ETH-USDT",
        )
    assert notional == 50.0


def test_override_with_alpha_hint_takes_min():
    """Hint $30 < override cap $70 → uses hint."""
    with _patch("BTC-USDT:fast-intraday=0.7"):
        notional = compute_notional(
            bucket="fast-intraday",
            alpha_metadata={"suggested_notional_usd": 30},
            confidence=1.0,
            asset="BTC-USDT",
        )
    assert notional == 30.0


def test_compute_notional_back_compat_no_asset():
    """Existing callers that don't pass asset still get bucket default."""
    notional = compute_notional(
        bucket="fast-intraday",
        alpha_metadata={},
        confidence=1.0,
    )
    assert notional == 50.0
