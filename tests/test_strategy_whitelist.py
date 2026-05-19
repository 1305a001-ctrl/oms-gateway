"""BULLETPROOF whitelist tests — added 2026-05-19 after a budget-cap race
condition let non-edge strategies fire $240+ of unauthorized trades in 9s.

The whitelist is the non-bypassable invariant: any alpha whose strategy
is not in the whitelist must be REJECTED at preflight, period. No
budget cap, no env flag, no race condition can defeat this layer.
"""
from datetime import UTC, datetime

import pytest

from oms_gateway.preflight import evaluate


def _snapshot(period: str, drawdown_pct: float) -> dict:
    return {
        "period": period,
        "drawdown_pct": drawdown_pct,
        "snapshot_at": datetime.now(UTC),
        "drawdown_usd": 0.0,
        "high_water_mark_usd": 10000.0,
    }


def _clean_risk():
    return {
        "daily": _snapshot("daily", 0.0),
        "weekly": _snapshot("weekly", 0.0),
        "monthly": _snapshot("monthly", 0.0),
        "total": _snapshot("total", 0.0),
    }


def test_whitelisted_strategy_passes_through(monkeypatch):
    """A strategy in the whitelist gets through the gate."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "live_strategy_whitelist_csv", "poly-chainlink-lag")

    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots=_clean_risk(),
    )
    assert decision.accept


def test_non_whitelisted_strategy_REJECTED(monkeypatch):
    """A strategy NOT in the whitelist gets HARD-REJECTED — even when
    everything else (halt, dd, budget) would have let it through.

    This is the test that would have prevented the 2026-05-19 disaster:
    premarket-top-taker-fdv NOT in whitelist → never reaches the live
    order path, period."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "live_strategy_whitelist_csv", "poly-chainlink-lag")

    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="poly-premarket-top-taker-fdv",
        risk_snapshots=_clean_risk(),
    )
    assert not decision.accept
    assert decision.reason == "strategy_not_whitelisted_for_live"
    assert decision.snapshot_used["strategy_slug"] == "poly-premarket-top-taker-fdv"
    assert "poly-chainlink-lag" in decision.snapshot_used["whitelist"]


def test_empty_whitelist_disables_check(monkeypatch):
    """Empty whitelist = back-compat path (no constraint). Lets all
    strategies through. ONLY use in tests / explicitly-opted-out
    deployments — production env MUST set the whitelist."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "live_strategy_whitelist_csv", "")

    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="any-strategy-at-all",
        risk_snapshots=_clean_risk(),
    )
    assert decision.accept


def test_whitelist_with_multiple_strategies(monkeypatch):
    """When operator validates a new strategy, they extend the CSV."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(
        settings,
        "live_strategy_whitelist_csv",
        "poly-chainlink-lag,poly-publisher-taker-long",
    )

    # First listed
    d1 = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots=_clean_risk(),
    )
    assert d1.accept

    # Second listed
    d2 = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-publisher-taker-long",
        risk_snapshots=_clean_risk(),
    )
    assert d2.accept

    # Not listed
    d3 = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-premarket-top-taker-fdv",
        risk_snapshots=_clean_risk(),
    )
    assert not d3.accept
    assert d3.reason == "strategy_not_whitelisted_for_live"


def test_whitelist_strips_whitespace(monkeypatch):
    """Operator might write 'a, b , c' — whitespace handled."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(
        settings,
        "live_strategy_whitelist_csv",
        " poly-chainlink-lag ,  poly-publisher-taker-long  ",
    )

    decision = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots=_clean_risk(),
    )
    assert decision.accept


def test_whitelist_takes_priority_over_halt(monkeypatch):
    """Whitelist check happens BEFORE halt check — so the rejection
    reason is `strategy_not_whitelisted_for_live`, not `system_halted`.
    This matters: if both gates fail, we want to know the whitelist
    issue first (operator action vs incident response)."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "live_strategy_whitelist_csv", "poly-chainlink-lag")

    decision = evaluate(
        halt_active=True,  # ALSO halted
        strategy_halt_active=False,
        strategy_slug="poly-premarket-top-taker-fdv",
        risk_snapshots=_clean_risk(),
    )
    assert not decision.accept
    assert decision.reason == "strategy_not_whitelisted_for_live"


def test_default_whitelist_is_chainlink_lag_only():
    """The DEFAULT in production must be exactly chainlink_lag — single
    strategy with proven data-stream edge. This guards against operator
    error: if env var is missing/unset, the safe default still blocks
    everything else.

    NOTE: tests/conftest.py overrides this to empty for unrelated tests.
    Here we read the Settings class default directly, NOT the runtime
    value."""
    from oms_gateway.settings import Settings
    default = Settings.model_fields["live_strategy_whitelist_csv"].default
    assert default == "poly-chainlink-lag", (
        f"Default whitelist = {default!r} — must be 'poly-chainlink-lag' "
        "as the safe production fallback after the 2026-05-19 incident"
    )


def test_no_strategy_slug_passed_REJECTED(monkeypatch):
    """If strategy_slug is None (caller bug, missing metadata), reject.
    Defense in depth — don't let unidentified alphas through."""
    from oms_gateway.settings import settings
    monkeypatch.setattr(settings, "live_strategy_whitelist_csv", "poly-chainlink-lag")

    decision = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug=None,
        risk_snapshots=_clean_risk(),
    )
    assert not decision.accept
    assert decision.reason == "strategy_not_whitelisted_for_live"
