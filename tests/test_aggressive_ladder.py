"""Aggressive vs conservative bankroll ladder tests."""
from __future__ import annotations

from oms_gateway import bankroll_aware_sizing as bas


def test_aggressive_ladder_is_2x_conservative():
    """At each tier index, aggressive caps should be exactly 2x conservative."""
    for i, (cons, agg) in enumerate(zip(
        bas.CONSERVATIVE_LADDER, bas.AGGRESSIVE_LADDER, strict=True,
    )):
        assert cons.pnl_threshold_usd == agg.pnl_threshold_usd, (
            f"tier {i}: thresholds must match"
        )
        # Strategy budget 2x
        assert agg.strategy_budget_usd == 2 * cons.strategy_budget_usd
        # Order notional 2x
        assert agg.order_notional_usd == 2 * cons.order_notional_usd


def test_active_ladder_default_conservative(monkeypatch):
    """Default flag OFF → conservative."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "aggressive_bankroll_ladder", False)
    assert bas.active_ladder() is bas.CONSERVATIVE_LADDER


def test_active_ladder_aggressive_when_enabled(monkeypatch):
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "aggressive_bankroll_ladder", True)
    assert bas.active_ladder() is bas.AGGRESSIVE_LADDER


def test_select_tier_uses_active_ladder(monkeypatch):
    """At +$500 PnL, conservative gives $400 budget, aggressive gives $800."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "aggressive_bankroll_ladder", False)
    cons_tier = bas.select_tier(500.0)
    assert cons_tier.strategy_budget_usd == 400.0

    monkeypatch.setattr(s.settings, "aggressive_bankroll_ladder", True)
    agg_tier = bas.select_tier(500.0)
    assert agg_tier.strategy_budget_usd == 800.0


def test_select_tier_t6_top_aggressive():
    """At $25k+ PnL on aggressive ladder, get $10k strategy budget."""
    tier = bas.select_tier(25000.0, ladder=bas.AGGRESSIVE_LADDER)
    assert tier.label == "T6_agg_top"
    assert tier.strategy_budget_usd == 10_000.0
    assert tier.order_notional_usd == 2400.0


def test_aggressive_ladder_labels_distinct():
    """All labels should have 'agg' marker to distinguish in logs."""
    for tier in bas.AGGRESSIVE_LADDER:
        assert "agg" in tier.label.lower()
