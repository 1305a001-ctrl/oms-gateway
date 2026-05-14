"""Tests for LP multiplier integration (alias resolution + payload parsing)."""
from __future__ import annotations

import json

import pytest

from oms_gateway.lp_multiplier import (
    LP_TRACKED_ASSETS,
    MAX_MULTIPLIER,
    MIN_MULTIPLIER,
    alpha_asset_to_lp_alias,
    fetch_multiplier,
    parse_multiplier_payload,
)

# ─── Alias resolution ────────────────────────────────────────────────


def test_alias_btc_variants() -> None:
    assert alpha_asset_to_lp_alias("BTC") == "btc"
    assert alpha_asset_to_lp_alias("BTC-USDT") == "btc"
    assert alpha_asset_to_lp_alias("BTC_USDT") == "btc"
    assert alpha_asset_to_lp_alias("BTC/USD") == "btc"
    assert alpha_asset_to_lp_alias("BTCUSDT") == "btc"
    assert alpha_asset_to_lp_alias("btc-usdt") == "btc"


def test_alias_all_tracked_assets_resolve() -> None:
    for asset in LP_TRACKED_ASSETS:
        # Bare alias
        assert alpha_asset_to_lp_alias(asset.upper()) == asset
        # With USDT pair
        assert alpha_asset_to_lp_alias(f"{asset.upper()}-USDT") == asset


def test_alias_unknown_returns_none() -> None:
    assert alpha_asset_to_lp_alias("AAPL") is None
    assert alpha_asset_to_lp_alias("NVDA") is None
    assert alpha_asset_to_lp_alias("EUR/USD") is None
    assert alpha_asset_to_lp_alias("AVAX-USDT") is None
    assert alpha_asset_to_lp_alias("LINK") is None


def test_alias_none_or_empty() -> None:
    assert alpha_asset_to_lp_alias(None) is None
    assert alpha_asset_to_lp_alias("") is None


def test_alias_longest_match_wins() -> None:
    """If two aliases share a prefix, the longer match should win."""
    # No actual conflicts in our 7-asset set, but verify the algorithm
    # still works under the sort-by-length rule.
    assert alpha_asset_to_lp_alias("HYPE-USDT") == "hype"
    assert alpha_asset_to_lp_alias("DOGE") == "doge"


# ─── Payload parsing ─────────────────────────────────────────────────


def _payload(multiplier: float, age_sec: float = 0.0, computed_at: float | None = None) -> str:
    if computed_at is None:
        import time
        computed_at = time.time() - age_sec
    return json.dumps({
        "asset": "btc",
        "multiplier": multiplier,
        "velocity_penalty": 0.0,
        "correlation_penalty": 0.0,
        "shock_penalty": 0.0,
        "shock_active": False,
        "reason": "test",
        "computed_at_unix": computed_at,
    })


def test_parse_calm_multiplier() -> None:
    assert parse_multiplier_payload(_payload(1.0)) == 1.0
    assert parse_multiplier_payload(_payload(0.95)) == 0.95
    assert parse_multiplier_payload(_payload(0.85)) == 0.85


def test_parse_shock_multiplier_clamped() -> None:
    # Below MIN_MULTIPLIER → clamped up
    assert parse_multiplier_payload(_payload(0.4)) == MIN_MULTIPLIER
    # Above MAX_MULTIPLIER → clamped down
    assert parse_multiplier_payload(_payload(3.0)) == MAX_MULTIPLIER


def test_parse_stale_payload_defaults_to_one() -> None:
    """Multiplier older than max_age_sec returns 1.0 (don't act on old data)."""
    stale = _payload(0.80, age_sec=120.0)   # 2 min old
    assert parse_multiplier_payload(stale, max_age_sec=30.0) == 1.0


def test_parse_fresh_payload_at_boundary() -> None:
    """Just inside max_age window → use the value."""
    fresh = _payload(0.80, age_sec=10.0)
    assert parse_multiplier_payload(fresh, max_age_sec=30.0) == 0.80


def test_parse_empty_or_none_returns_one() -> None:
    assert parse_multiplier_payload(None) == 1.0
    assert parse_multiplier_payload("") == 1.0
    assert parse_multiplier_payload(b"") == 1.0


def test_parse_malformed_json_returns_one() -> None:
    assert parse_multiplier_payload("not-json") == 1.0
    assert parse_multiplier_payload("{not valid") == 1.0


def test_parse_missing_multiplier_field_returns_one() -> None:
    raw = json.dumps({"asset": "btc"})   # no multiplier
    assert parse_multiplier_payload(raw) == 1.0


def test_parse_non_numeric_multiplier_returns_one() -> None:
    raw = json.dumps({"multiplier": "0.85"})   # string not float
    # Should parse since float("0.85") works
    assert parse_multiplier_payload(raw) == 0.85
    raw = json.dumps({"multiplier": "not-a-number"})
    assert parse_multiplier_payload(raw) == 1.0


def test_parse_bytes_input() -> None:
    payload = _payload(0.90).encode("utf-8")
    assert parse_multiplier_payload(payload) == 0.90


# ─── Async fetch ─────────────────────────────────────────────────────


class _FakeRedis:
    def __init__(self, payload: str | None = None, raise_on_get: bool = False):
        self.payload = payload
        self.raise_on_get = raise_on_get
        self.get_calls: list[str] = []

    async def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        if self.raise_on_get:
            raise RuntimeError("simulated redis down")
        return self.payload


@pytest.mark.asyncio
async def test_fetch_tracked_asset_returns_payload_value() -> None:
    rc = _FakeRedis(payload=_payload(0.85))
    result = await fetch_multiplier(rc, "BTC-USDT")
    assert result == 0.85
    assert rc.get_calls == ["liquidity-pulse:btc:multiplier"]


@pytest.mark.asyncio
async def test_fetch_untracked_asset_skips_redis() -> None:
    rc = _FakeRedis(payload=_payload(0.50))
    result = await fetch_multiplier(rc, "AAPL")
    assert result == 1.0
    assert rc.get_calls == []   # Never even hit Redis


@pytest.mark.asyncio
async def test_fetch_none_asset_returns_one() -> None:
    rc = _FakeRedis(payload=_payload(0.50))
    result = await fetch_multiplier(rc, None)
    assert result == 1.0
    assert rc.get_calls == []


@pytest.mark.asyncio
async def test_fetch_redis_error_defaults_to_one() -> None:
    rc = _FakeRedis(raise_on_get=True)
    result = await fetch_multiplier(rc, "BTC-USDT")
    assert result == 1.0   # don't crash sizing on Redis hiccup


@pytest.mark.asyncio
async def test_fetch_missing_key_returns_one() -> None:
    rc = _FakeRedis(payload=None)
    result = await fetch_multiplier(rc, "ETH-USDT")
    assert result == 1.0
