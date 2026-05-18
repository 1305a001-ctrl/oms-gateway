"""Per-strategy per-ORDER cap tests (2026-05-18).

Distinct from `strategy_budget_overrides` (total exposure cap). The
order cap bounds each individual order so a strategy can accumulate
its full budget via many small orders rather than refusing the first
big one at the adapter ceiling.
"""
from unittest.mock import patch

from oms_gateway.sizing import _strategy_order_cap_usd, compute_notional

# ─── _strategy_order_cap_usd ───────────────────────────────────────


def test_order_cap_none_when_slug_missing():
    assert _strategy_order_cap_usd(None) is None
    assert _strategy_order_cap_usd("") is None


def test_order_cap_none_when_overrides_empty():
    with patch("oms_gateway.sizing.settings") as s:
        s.strategy_order_cap_overrides = ""
        assert _strategy_order_cap_usd("any-slug") is None


def test_order_cap_none_when_slug_not_in_overrides():
    with patch("oms_gateway.sizing.settings") as s:
        s.strategy_order_cap_overrides = "other-slug=20"
        assert _strategy_order_cap_usd("missing-slug") is None


def test_order_cap_resolves_override():
    with patch("oms_gateway.sizing.settings") as s:
        s.strategy_order_cap_overrides = "poly-publisher-taker-long=20"
        assert _strategy_order_cap_usd("poly-publisher-taker-long") == 20.0


def test_order_cap_multiple_overrides():
    with patch("oms_gateway.sizing.settings") as s:
        s.strategy_order_cap_overrides = (
            "poly-publisher-taker-long=20,"
            "poly-sell-wings=50,"
            "btc-liq-cascade=100"
        )
        assert _strategy_order_cap_usd("poly-publisher-taker-long") == 20.0
        assert _strategy_order_cap_usd("poly-sell-wings") == 50.0
        assert _strategy_order_cap_usd("btc-liq-cascade") == 100.0
        assert _strategy_order_cap_usd("unrelated-strategy") is None


def test_order_cap_zero_or_negative_treated_as_unset():
    with patch("oms_gateway.sizing.settings") as s:
        s.strategy_order_cap_overrides = "zero=0,negative=-5"
        assert _strategy_order_cap_usd("zero") is None
        assert _strategy_order_cap_usd("negative") is None


# ─── compute_notional with order cap ───────────────────────────────


def _ctx(strategy_slug=None, strategy_exposure=0.0, **overrides):
    """Build a compute_notional call with sensible defaults."""
    base = dict(
        bucket="poly-bet",
        alpha_metadata={},
        confidence=0.95,
        asset="ethereum-above-2500-on-may-18",
        lp_multiplier=1.0,
        strategy_slug=strategy_slug,
        strategy_open_exposure_usd=strategy_exposure,
    )
    base.update(overrides)
    return base


def test_compute_notional_caps_at_order_override():
    """With order cap $20, intent sizes to $20 even when bucket math says larger."""
    with patch("oms_gateway.sizing.settings") as s:
        s.paper_account_equity_usd = 100_000.0
        s.default_per_trade_pct = 1.0
        s.bucket_size_pct_max = {"poly-bet": 2.0}
        s.bucket_size_overrides = ""
        s.strategy_budget_overrides = "poly-publisher-taker-long=400"
        s.default_strategy_budget_usd = 0.0
        s.strategy_order_cap_overrides = "poly-publisher-taker-long=20"

        # bucket_cap_usd = 2% × 100k = 2000; scaled = 2000 × 0.95 = 1900
        # strategy_budget cap = 400, remaining = 400 (exposure=0)
        # → without order cap, would be 400
        # → with order cap = 20, final = 20
        notional = compute_notional(
            **_ctx(strategy_slug="poly-publisher-taker-long")
        )
        assert notional == 20.0


def test_compute_notional_no_change_when_order_cap_absent():
    """Without the override, sizing follows normal budget/bucket math."""
    with patch("oms_gateway.sizing.settings") as s:
        s.paper_account_equity_usd = 100_000.0
        s.default_per_trade_pct = 1.0
        s.bucket_size_pct_max = {"poly-bet": 2.0}
        s.bucket_size_overrides = ""
        s.strategy_budget_overrides = "poly-publisher-taker-long=400"
        s.default_strategy_budget_usd = 0.0
        s.strategy_order_cap_overrides = ""

        notional = compute_notional(
            **_ctx(strategy_slug="poly-publisher-taker-long")
        )
        # scaled = $1900, capped at budget remaining $400 = $400
        assert notional == 400.0


def test_compute_notional_order_cap_applied_after_budget_cap():
    """Order cap takes effect after budget cap; whichever is smaller wins."""
    with patch("oms_gateway.sizing.settings") as s:
        s.paper_account_equity_usd = 100_000.0
        s.default_per_trade_pct = 1.0
        s.bucket_size_pct_max = {"poly-bet": 2.0}
        s.bucket_size_overrides = ""
        s.strategy_budget_overrides = "poly-publisher-taker-long=400"
        s.default_strategy_budget_usd = 0.0
        s.strategy_order_cap_overrides = "poly-publisher-taker-long=50"

        # Existing exposure $390 → budget remaining = $10
        # Order cap = $50 → does NOT lower further (10 < 50)
        notional = compute_notional(
            **_ctx(
                strategy_slug="poly-publisher-taker-long",
                strategy_exposure=390.0,
            )
        )
        assert notional == 10.0


def test_compute_notional_order_cap_smaller_than_remaining_budget():
    """Order cap wins when smaller than budget remaining."""
    with patch("oms_gateway.sizing.settings") as s:
        s.paper_account_equity_usd = 100_000.0
        s.default_per_trade_pct = 1.0
        s.bucket_size_pct_max = {"poly-bet": 2.0}
        s.bucket_size_overrides = ""
        s.strategy_budget_overrides = "poly-publisher-taker-long=400"
        s.default_strategy_budget_usd = 0.0
        s.strategy_order_cap_overrides = "poly-publisher-taker-long=20"

        # Exposure $200 → budget remaining = $200
        # Order cap = $20 → wins
        notional = compute_notional(
            **_ctx(
                strategy_slug="poly-publisher-taker-long",
                strategy_exposure=200.0,
            )
        )
        assert notional == 20.0


def test_compute_notional_order_cap_does_not_apply_to_other_strategies():
    """Override is per-slug — other strategies unaffected."""
    with patch("oms_gateway.sizing.settings") as s:
        s.paper_account_equity_usd = 100_000.0
        s.default_per_trade_pct = 1.0
        s.bucket_size_pct_max = {"poly-bet": 2.0}
        s.bucket_size_overrides = ""
        s.strategy_budget_overrides = ""
        s.default_strategy_budget_usd = 0.0
        s.strategy_order_cap_overrides = "poly-publisher-taker-long=20"

        # Different strategy — no cap → normal sizing
        notional = compute_notional(**_ctx(strategy_slug="poly-sell-wings"))
        # scaled = 1900, no budget cap, no order cap → 1900
        assert notional == 1900.0
