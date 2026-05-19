"""Phase 2.8 — per-position cap preflight tests."""
from unittest.mock import patch

from oms_gateway.preflight import (
    Decision,
    ExistingPosition,
    _resolve_position_cap_usd,
    _would_breach_position_cap,
    evaluate,
)

# --- _resolve_position_cap_usd ---------------------------------------------

def test_cap_for_fast_intraday():
    """0.5% × 5x mult × $10k = $250."""
    cap = _resolve_position_cap_usd("fast-intraday")
    assert cap == 250.0


def test_cap_for_swing():
    """1.5% × 5x × $10k = $750."""
    cap = _resolve_position_cap_usd("swing")
    assert cap == 750.0


def test_cap_for_unknown_bucket_uses_default_per_trade_pct():
    """Unknown bucket → default_per_trade_pct (1%) × 5x × $10k = $500."""
    cap = _resolve_position_cap_usd("does-not-exist")
    assert cap == 500.0


def test_cap_for_none_bucket():
    cap = _resolve_position_cap_usd(None)
    assert cap == 500.0


# --- _would_breach_position_cap --------------------------------------------

def test_no_existing_under_cap():
    """First trade, $200 notional, fast-intraday cap $250 → pass."""
    result = _would_breach_position_cap(
        existing=None,
        alpha_direction="long",
        proposed_notional_usd=200,
        bucket="fast-intraday",
    )
    assert result is None


def test_no_existing_over_cap():
    """First trade $300 > $250 cap → reject."""
    result = _would_breach_position_cap(
        existing=None,
        alpha_direction="long",
        proposed_notional_usd=300,
        bucket="fast-intraday",
    )
    assert result is not None
    assert result.accept is False
    assert result.reason == "position_cap_exceeded"


def test_existing_long_scale_in_under_cap():
    """Existing $150 long + $50 trade = $200 < $250 cap → pass."""
    existing = ExistingPosition(
        qty=1.5, side="long", mark_price=100.0, avg_entry_price=100.0,
    )
    result = _would_breach_position_cap(
        existing=existing,
        alpha_direction="long",
        proposed_notional_usd=50,
        bucket="fast-intraday",
    )
    assert result is None


def test_existing_long_scale_in_over_cap():
    """Existing $200 long + $100 trade = $300 > $250 cap → REJECT."""
    existing = ExistingPosition(
        qty=2.0, side="long", mark_price=100.0, avg_entry_price=100.0,
    )
    result = _would_breach_position_cap(
        existing=existing,
        alpha_direction="long",
        proposed_notional_usd=100,
        bucket="fast-intraday",
    )
    assert result is not None
    assert result.accept is False
    assert result.reason == "position_cap_exceeded"
    assert result.snapshot_used["would_be_notional_usd"] == 300.0


def test_opposite_side_always_passes_close():
    """Existing $1000 long + $500 SHORT = closing trade. Pass even though
    sum-notional would breach cap."""
    existing = ExistingPosition(
        qty=10.0, side="long", mark_price=100.0, avg_entry_price=100.0,
    )
    result = _would_breach_position_cap(
        existing=existing,
        alpha_direction="short",  # opposite
        proposed_notional_usd=500,
        bucket="fast-intraday",
    )
    assert result is None


def test_flat_alpha_always_passes():
    """Flat = explicit close. Pass regardless of existing size."""
    existing = ExistingPosition(
        qty=999, side="long", mark_price=100.0, avg_entry_price=100.0,
    )
    result = _would_breach_position_cap(
        existing=existing,
        alpha_direction="flat",
        proposed_notional_usd=None,
        bucket="fast-intraday",
    )
    assert result is None


def test_runaway_tsla_scenario_rejected():
    """The exact pattern that caused the overnight bug:
    existing 470-share short × $385 = $181k. Another $300 sell would push to $181.3k
    — way past $250 cap. Reject."""
    existing = ExistingPosition(
        qty=470.0, side="short", mark_price=385.0, avg_entry_price=385.0,
    )
    result = _would_breach_position_cap(
        existing=existing,
        alpha_direction="short",
        proposed_notional_usd=300,
        bucket="fast-intraday",
    )
    assert result is not None
    assert result.accept is False
    assert result.snapshot_used["would_be_notional_usd"] > 100_000


def test_existing_with_no_mark_uses_avg_entry():
    """Pre-MTM-tick: existing position has no mark_price yet. Falls back
    to avg_entry for the would-be calc."""
    existing = ExistingPosition(
        qty=1.0, side="long", mark_price=None, avg_entry_price=100.0,
    )
    result = _would_breach_position_cap(
        existing=existing,
        alpha_direction="long",
        proposed_notional_usd=200,
        bucket="fast-intraday",  # cap $250
    )
    # Existing 1 × $100 + $200 trade = $300 > $250 → reject.
    assert result is not None
    assert result.accept is False


def test_zero_notional_passes():
    """Defensive: 0 / negative notional = no real trade. Pass."""
    assert _would_breach_position_cap(
        existing=None, alpha_direction="long",
        proposed_notional_usd=0, bucket="fast-intraday",
    ) is None
    assert _would_breach_position_cap(
        existing=None, alpha_direction="long",
        proposed_notional_usd=-50, bucket="fast-intraday",
    ) is None


def test_swing_bucket_higher_cap():
    """swing: 1.5% × 5x × $10k = $750. A $700 single trade is fine."""
    result = _would_breach_position_cap(
        existing=None,
        alpha_direction="long",
        proposed_notional_usd=700,
        bucket="swing",
    )
    assert result is None


def test_position_cap_mult_configurable():
    """Override the multiplier — 1x means a position can't scale beyond a single trade."""
    with patch("oms_gateway.preflight.settings.bucket_position_cap_mult", 1.0):
        # fast-intraday cap = 0.5% × 1x × $10k = $50
        result = _would_breach_position_cap(
            existing=None, alpha_direction="long",
            proposed_notional_usd=60, bucket="fast-intraday",
        )
        assert result is not None and result.accept is False


# --- evaluate() integration -------------------------------------------------

def test_evaluate_position_cap_blocks_after_drawdown_passes(monkeypatch):
    """Existing $1000 long, fast-intraday cap $250 → cap breach takes priority.

    Note (2026-05-20): the duplicate-position guard would otherwise reject
    this with reason='duplicate_position_same_direction'. Opt the test
    strategy into scale-in so position_cap is the next gate to trip."""
    from oms_gateway import settings as s
    monkeypatch.setattr(
        s.settings, "allow_same_direction_scale_in_strategies_csv", "test-strat",
    )
    existing = ExistingPosition(
        qty=10.0, side="long", mark_price=100.0, avg_entry_price=100.0,
    )
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="test-strat",
        risk_snapshots={},
        existing_position=existing,
        alpha_direction="long",
        proposed_notional_usd=50,
        bucket="fast-intraday",
    )
    assert decision.accept is False
    assert decision.reason == "position_cap_exceeded"


def test_evaluate_halt_takes_priority_over_position_cap():
    """system halt fires first, even when position cap would also breach."""
    existing = ExistingPosition(
        qty=999, side="long", mark_price=100.0, avg_entry_price=100.0,
    )
    decision = evaluate(
        halt_active=True,
        strategy_halt_active=False,
        strategy_slug="test-strat",
        risk_snapshots={},
        existing_position=existing,
        alpha_direction="long",
        proposed_notional_usd=50,
        bucket="fast-intraday",
    )
    assert decision.accept is False
    assert decision.reason == "system_halted"


def test_evaluate_back_compat_no_position_args():
    """Existing test pattern: only halt + risk_snapshots passed. Still works."""
    decision: Decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="test-strat",
        risk_snapshots={},
    )
    assert decision.accept is True
