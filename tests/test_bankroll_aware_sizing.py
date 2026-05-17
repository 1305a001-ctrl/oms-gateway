"""Tests for the bankroll-aware sizing primitive (pure helpers)."""
from __future__ import annotations

import time

from oms_gateway import bankroll_aware_sizing as bas


# ─── Tier selection ────────────────────────────────────────────────────


def test_select_tier_seed_at_zero():
    t = bas.select_tier(0.0)
    assert t.label == "T0_seed"
    assert t.strategy_budget_usd == 200.0
    assert t.order_notional_usd == 50.0


def test_select_tier_seed_when_negative_pnl():
    t = bas.select_tier(-100.0)
    assert t.label == "T0_seed"


def test_select_tier_t1_at_threshold():
    t = bas.select_tier(500.0)
    assert t.label == "T1_first_profit"
    assert t.strategy_budget_usd == 400.0
    assert t.order_notional_usd == 100.0


def test_select_tier_t1_above():
    t = bas.select_tier(1400.0)
    assert t.label == "T1_first_profit"


def test_select_tier_t2():
    t = bas.select_tier(1500.0)
    assert t.label == "T2_proven"


def test_select_tier_top_tier():
    t = bas.select_tier(50000.0)
    assert t.label == "T6_top"
    assert t.strategy_budget_usd == 5000.0
    assert t.order_notional_usd == 1200.0


def test_select_tier_ratchets_with_realistic_path():
    """The user's projected $500 → $700 → $980 → $1372 → $1921 → $2690 → $3766 path."""
    # PnL = bankroll - 500 (initial)
    paths = [
        (200, "T0_seed"),       # +$200 → still T0
        (500, "T1_first_profit"),  # +$500 → T1
        (1000, "T1_first_profit"),
        (1500, "T2_proven"),
        (2500, "T2_proven"),
        (3000, "T3_compound"),
        (3266, "T3_compound"),  # the $3266 PnL point
    ]
    for pnl, expected in paths:
        assert bas.select_tier(pnl).label == expected, f"pnl={pnl}"


# ─── State payload ─────────────────────────────────────────────────────


def test_to_state_payload_shape():
    tier = bas.select_tier(1000.0)
    state = bas.to_state_payload(realized_pnl_usd=1000.0, tier=tier, now_unix=1700000000.0)
    assert state["realized_pnl_usd"] == 1000.0
    assert state["tier_label"] == "T1_first_profit"
    assert state["strategy_budget_usd"] == 400.0
    assert state["order_notional_usd"] == 100.0
    assert state["refresh_at_unix"] == 1700000000.0


# ─── Freshness ─────────────────────────────────────────────────────────


def test_is_state_fresh_recent_passes():
    s = {"refresh_at_unix": time.time() - 30}
    assert bas.is_state_fresh(s, max_age_sec=600) is True


def test_is_state_fresh_old_fails():
    s = {"refresh_at_unix": time.time() - 1000}
    assert bas.is_state_fresh(s, max_age_sec=600) is False


def test_is_state_fresh_missing_ts():
    assert bas.is_state_fresh({}, max_age_sec=600) is False


def test_is_state_fresh_garbage_ts():
    assert bas.is_state_fresh({"refresh_at_unix": "not a number"}, max_age_sec=600) is False


# ─── effective_* helpers ───────────────────────────────────────────────


def test_effective_disabled_returns_none(monkeypatch):
    from oms_gateway import settings as st_mod
    monkeypatch.setattr(st_mod.settings, "bankroll_aware_sizing_enabled", False)
    s = bas.to_state_payload(realized_pnl_usd=5000, tier=bas.DEFAULT_LADDER[3])
    assert bas.effective_strategy_budget(s) is None
    assert bas.effective_order_notional(s) is None


def test_effective_enabled_with_fresh_state(monkeypatch):
    from oms_gateway import settings as st_mod
    monkeypatch.setattr(st_mod.settings, "bankroll_aware_sizing_enabled", True)
    s = bas.to_state_payload(realized_pnl_usd=5000, tier=bas.DEFAULT_LADDER[3])
    assert bas.effective_strategy_budget(s) == 900.0
    assert bas.effective_order_notional(s) == 225.0


def test_effective_enabled_with_stale_state_returns_none(monkeypatch):
    from oms_gateway import settings as st_mod
    monkeypatch.setattr(st_mod.settings, "bankroll_aware_sizing_enabled", True)
    stale = bas.to_state_payload(
        realized_pnl_usd=5000,
        tier=bas.DEFAULT_LADDER[3],
        now_unix=time.time() - 10000,
    )
    assert bas.effective_strategy_budget(stale) is None
    assert bas.effective_order_notional(stale) is None


def test_effective_enabled_with_none_state_returns_none(monkeypatch):
    from oms_gateway import settings as st_mod
    monkeypatch.setattr(st_mod.settings, "bankroll_aware_sizing_enabled", True)
    assert bas.effective_strategy_budget(None) is None
    assert bas.effective_order_notional(None) is None


# ─── Ladder invariants ─────────────────────────────────────────────────


def test_ladder_is_monotonic_ascending():
    """Thresholds + caps both grow with each tier — operator can rely on this."""
    prev = bas.DEFAULT_LADDER[0]
    for tier in bas.DEFAULT_LADDER[1:]:
        assert tier.pnl_threshold_usd > prev.pnl_threshold_usd
        assert tier.strategy_budget_usd >= prev.strategy_budget_usd
        assert tier.order_notional_usd >= prev.order_notional_usd
        prev = tier


def test_ladder_starts_at_zero():
    """T0 must be at threshold 0 so any new operator gets a sane seed cap."""
    assert bas.DEFAULT_LADDER[0].pnl_threshold_usd == 0.0
