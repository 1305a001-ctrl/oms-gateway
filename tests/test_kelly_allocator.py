"""Kelly capital allocator — pure helper tests."""
from __future__ import annotations

from oms_gateway import kelly_allocator as kelly


def test_kelly_too_few_returns_neutral():
    raw, disc, capped = kelly.compute_kelly_fraction(returns=[0.01, 0.02])
    assert raw == 0.0
    assert disc == 0.0
    assert capped == 1.0


def test_kelly_zero_variance_neutral():
    """Identical returns → var=0 → neutral."""
    raw, disc, capped = kelly.compute_kelly_fraction(
        returns=[0.01, 0.01, 0.01, 0.01, 0.01],
    )
    assert capped == 1.0


def test_kelly_high_edge_high_multiplier():
    """Strategy with high mean / low variance → high Kelly."""
    profitable = [0.05, 0.04, 0.06, 0.05, 0.04, 0.05, 0.06]
    _raw, _disc, capped = kelly.compute_kelly_fraction(returns=profitable)
    assert capped > 1.0  # gets MORE budget than baseline


def test_kelly_losing_strategy_low_multiplier():
    """Negative mean → negative raw Kelly → clamped to floor."""
    losing = [-0.05, -0.04, -0.06, -0.05, -0.04, -0.05, -0.06]
    _raw, _disc, capped = kelly.compute_kelly_fraction(returns=losing)
    assert capped == kelly.DEFAULT_FLOOR_MULTIPLIER  # gets minimum


def test_kelly_caps_at_ceiling():
    """Ultra-high-edge strategies don't get unbounded allocation."""
    super_profitable = [0.1] * 5 + [0.11] + [0.09]
    _raw, _disc, capped = kelly.compute_kelly_fraction(
        returns=super_profitable, ceiling=3.0,
    )
    assert capped <= 3.0


def test_kelly_caps_at_floor():
    _raw, _disc, capped = kelly.compute_kelly_fraction(
        returns=[-1, -2, -3, -2, -1], floor=0.25,
    )
    assert capped == 0.25


def test_compute_allocation_basic():
    trades = [(50.0, 1000.0), (40.0, 1000.0), (60.0, 1000.0),
              (45.0, 1000.0), (55.0, 1000.0), (50.0, 1000.0)]
    a = kelly.compute_allocation(slug="winning", trades=trades)
    assert a.n_trades == 6
    assert a.mean_return > 0
    assert a.capped_multiplier > 1.0


def test_compute_allocation_too_few_trades_neutral():
    a = kelly.compute_allocation(
        slug="new", trades=[(50, 1000), (40, 1000)],
    )
    assert a.capped_multiplier == 1.0


def test_compute_allocation_skips_zero_size():
    """Trades with size_usd=0 must be filtered (undefined return)."""
    trades = [(50, 1000), (10, 0), (40, 1000), (20, 0), (30, 1000),
              (45, 0), (35, 1000), (55, 1000)]
    a = kelly.compute_allocation(slug="test", trades=trades)
    assert a.n_trades == 5    # 8 total - 3 zero-size
