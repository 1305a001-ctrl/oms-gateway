"""Tests for the Phase 2.9 concentration guard — bucket + cluster caps.

Pure helpers (`cluster_for`, `cluster_sql_filter`) get unit-tested in
isolation. The full `evaluate()` path gets exercised with synthetic
exposure values to assert the bucket / cluster breaches reject.
"""
from oms_gateway.preflight import (
    cluster_for,
    cluster_sql_filter,
    evaluate,
)
from oms_gateway.settings import settings


def test_cluster_for_polymarket_groups_threshold_ladder():
    a = cluster_for("polymarket", "bitcoin-above-78k-on-may-9")
    b = cluster_for("polymarket", "bitcoin-above-82k-on-may-9")
    c = cluster_for("polymarket", "bitcoin-up-or-down-on-may-9-2026")
    assert a == b == c == "poly:bitcoin"


def test_cluster_for_polymarket_separates_underlyings():
    assert cluster_for("polymarket", "bitcoin-above-80k") != cluster_for(
        "polymarket", "ethereum-above-3k"
    )


def test_cluster_for_binance_groups_quote_pairs():
    assert cluster_for("binance", "BTC-USDT") == "crypto:BTC"
    assert cluster_for("binance", "BTC-USDC") == "crypto:BTC"
    assert cluster_for("binance", "ETH-USDT") == "crypto:ETH"


def test_cluster_for_alpaca_uses_ticker():
    assert cluster_for("alpaca", "AAPL") == "stocks:AAPL"
    assert cluster_for("alpaca", "tsla") == "stocks:TSLA"


def test_cluster_for_handles_empty():
    assert cluster_for("", "") == ":?"
    assert cluster_for("polymarket", "") == "polymarket:?"


def test_cluster_sql_filter_polymarket():
    f = cluster_sql_filter("poly:bitcoin")
    assert f == ("polymarket", "bitcoin-%", "bitcoin")


def test_cluster_sql_filter_binance():
    f = cluster_sql_filter("crypto:BTC")
    assert f == ("binance", "BTC-%", "BTC")


def test_cluster_sql_filter_alpaca_exact_only():
    f = cluster_sql_filter("stocks:AAPL")
    # No prefix-LIKE; match exact ticker only.
    assert f is not None
    assert f[0] == "alpaca"
    assert f[2] == "AAPL"


def test_cluster_sql_filter_unknown():
    assert cluster_sql_filter("") is None
    assert cluster_sql_filter("not-a-cluster") is None
    assert cluster_sql_filter("foo:bar") is None


def test_evaluate_rejects_when_bucket_exposure_would_breach():
    # poly-bet bucket cap = 20% of 10k equity = $2000.
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="poly-sell-wings",
        risk_snapshots={},
        alpha_direction="long",
        proposed_notional_usd=300.0,
        bucket="poly-bet",
        bucket_open_exposure_usd=1900.0,  # 1900 + 300 > 2000 cap
    )
    assert not decision.accept
    assert decision.reason == "bucket_exposure_cap_exceeded"
    assert decision.snapshot_used["bucket"] == "poly-bet"
    assert decision.snapshot_used["would_be_exposure_usd"] == 2200.0


def test_evaluate_accepts_when_bucket_under_cap():
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="poly-sell-wings",
        risk_snapshots={},
        alpha_direction="long",
        proposed_notional_usd=50.0,
        bucket="poly-bet",
        bucket_open_exposure_usd=400.0,
    )
    assert decision.accept


def test_evaluate_rejects_when_cluster_exposure_would_breach():
    # cluster cap = 8% of 10k = $800.
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="poly-sell-wings",
        risk_snapshots={},
        alpha_direction="long",
        proposed_notional_usd=200.0,
        bucket="poly-bet",
        bucket_open_exposure_usd=0.0,
        cluster="poly:bitcoin",
        cluster_open_exposure_usd=700.0,  # 700 + 200 > 800
    )
    assert not decision.accept
    assert decision.reason == "cluster_exposure_cap_exceeded"
    assert decision.snapshot_used["cluster"] == "poly:bitcoin"


def test_evaluate_close_trades_skip_concentration_guard():
    # Closing trades must always pass concentration guards so we can exit.
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="poly-sell-wings",
        risk_snapshots={},
        alpha_direction="flat",
        proposed_notional_usd=None,
        bucket="poly-bet",
        bucket_open_exposure_usd=99_999.0,
        cluster="poly:bitcoin",
        cluster_open_exposure_usd=99_999.0,
    )
    assert decision.accept


def test_evaluate_disabled_caps_pass_through():
    # Unknown bucket name → no per-bucket exposure cap entry, so the
    # bucket guard is a no-op (proposed notional small enough to clear
    # the per-position cap default of $500).
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="x",
        risk_snapshots={},
        alpha_direction="long",
        proposed_notional_usd=100.0,
        bucket="not-a-real-bucket",
        bucket_open_exposure_usd=1_000_000.0,
    )
    assert decision.accept


def test_evaluate_position_cap_runs_before_concentration():
    # Per-position cap rejects first if both would breach — keeps the
    # rejection reason close to the actual cause for the operator.
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="x",
        risk_snapshots={},
        alpha_direction="long",
        proposed_notional_usd=settings.paper_account_equity_usd,  # huge
        bucket="poly-bet",
        bucket_open_exposure_usd=settings.paper_account_equity_usd,
    )
    assert not decision.accept
    # position_cap_exceeded comes from per-position check (first to fire).
    assert decision.reason == "position_cap_exceeded"
