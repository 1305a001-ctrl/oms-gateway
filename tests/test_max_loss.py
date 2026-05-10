"""Tests for max-loss exposure cap (the 2026-05-09 tennis loss fix)."""
from __future__ import annotations

import pytest

from oms_gateway.max_loss import (
    check_max_loss_for_short_prediction,
    max_loss_for_short_prediction,
)


# Pure max-loss math


def test_max_loss_short_at_low_price():
    """Tennis loss replication: 43547 short at $0.093 → max loss $39.5k."""
    loss = max_loss_for_short_prediction(qty=43_547.84, entry_price=0.093)
    assert loss == pytest.approx(43_547.84 * 0.907, rel=1e-4)
    assert loss > 39_000  # confirm we'd flag this


def test_max_loss_short_at_high_price():
    """Short at $0.95 (close to YES) → tiny max loss."""
    loss = max_loss_for_short_prediction(qty=1000, entry_price=0.95)
    assert loss == pytest.approx(50.0)


def test_max_loss_short_at_50():
    """Short at $0.50 (coin flip) → max loss = entry notional."""
    loss = max_loss_for_short_prediction(qty=1000, entry_price=0.50)
    assert loss == pytest.approx(500.0)


def test_max_loss_zero_for_invalid_inputs():
    assert max_loss_for_short_prediction(qty=0, entry_price=0.5) == 0.0
    assert max_loss_for_short_prediction(qty=100, entry_price=0) == 0.0
    assert max_loss_for_short_prediction(qty=100, entry_price=1.0) == 0.0  # already settled
    assert max_loss_for_short_prediction(qty=100, entry_price=1.5) == 0.0  # impossible


# check_max_loss_for_short_prediction — gate logic


def test_check_blocks_the_tennis_trade():
    """Reproduce the 2026-05-09 case with reasonable per-position cap."""
    reason = check_max_loss_for_short_prediction(
        qty=43_547.84,
        entry_price=0.093,
        per_position_cap_usd=5_000.0,  # the cap setting at the time
    )
    assert reason is not None
    assert "max_loss_breach" in reason
    assert "$39" in reason  # max loss number in message


def test_check_allows_short_at_high_price_within_cap():
    """1000 shares short at $0.95 → max loss $50, well within $5k cap."""
    reason = check_max_loss_for_short_prediction(
        qty=1000, entry_price=0.95, per_position_cap_usd=5_000.0,
    )
    assert reason is None


def test_check_disabled_when_cap_zero_or_negative():
    """Per-position cap of 0 or negative = disabled (no gate)."""
    reason = check_max_loss_for_short_prediction(
        qty=10_000, entry_price=0.05, per_position_cap_usd=0.0,
    )
    assert reason is None


def test_check_long_at_50_short_at_50_symmetric():
    """At entry=$0.50, max-loss for short = entry notional."""
    # Short 1000 at $0.50 → max loss $500. Cap $1k → no breach.
    reason = check_max_loss_for_short_prediction(
        qty=1000, entry_price=0.50, per_position_cap_usd=1_000.0,
    )
    assert reason is None
    # Same trade with cap $400 → breach.
    reason = check_max_loss_for_short_prediction(
        qty=1000, entry_price=0.50, per_position_cap_usd=400.0,
    )
    assert reason is not None


def test_check_at_boundary_just_passes():
    """Max loss exactly equal to cap should pass (we use > not >=)."""
    # 1000 short at $0.50 → max loss $500. Cap $500 → exactly equal → pass.
    reason = check_max_loss_for_short_prediction(
        qty=1000, entry_price=0.50, per_position_cap_usd=500.0,
    )
    assert reason is None


def test_check_breach_message_includes_diagnostic_numbers():
    reason = check_max_loss_for_short_prediction(
        qty=10_000, entry_price=0.10, per_position_cap_usd=5_000.0,
    )
    assert reason is not None
    # Caller should be able to read qty, entry, and the cap from the reason
    assert "qty=10000" in reason
    assert "entry=0.1000" in reason
    assert "$5000" in reason
