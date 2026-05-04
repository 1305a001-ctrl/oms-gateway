"""Pure-function preflight tests — no I/O, no mocks."""
from datetime import UTC, datetime

from oms_gateway.preflight import evaluate


def _snapshot(period: str, drawdown_pct: float) -> dict:
    return {
        "period": period,
        "drawdown_pct": drawdown_pct,
        "snapshot_at": datetime.now(UTC),
        "drawdown_usd": 0.0,
        "high_water_mark_usd": 10000.0,
    }


def test_accept_clean_state():
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="btc-momentum",
        risk_snapshots={
            "daily": _snapshot("daily", 1.0),
            "weekly": _snapshot("weekly", 2.0),
            "monthly": _snapshot("monthly", 3.0),
            "total": _snapshot("total", 5.0),
        },
    )
    assert decision.accept
    assert decision.reason is None
    assert decision.period_breached is None


def test_reject_when_system_halted():
    decision = evaluate(
        halt_active=True,
        strategy_halt_active=False,
        strategy_slug="btc-momentum",
        risk_snapshots={},
    )
    assert not decision.accept
    assert decision.reason == "system_halted"


def test_reject_when_strategy_halted():
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=True,
        strategy_slug="btc-momentum",
        risk_snapshots={},
    )
    assert not decision.accept
    assert decision.reason == "strategy_halted"
    assert decision.snapshot_used.get("strategy_slug") == "btc-momentum"


def test_reject_daily_dd_breach():
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="btc-momentum",
        risk_snapshots={"daily": _snapshot("daily", 5.5)},
    )
    assert not decision.accept
    assert decision.reason == "daily_dd_breached"
    assert decision.period_breached == "daily"


def test_reject_total_dd_breach_only():
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="btc-momentum",
        risk_snapshots={
            "daily": _snapshot("daily", 1.0),
            "weekly": _snapshot("weekly", 2.0),
            "monthly": _snapshot("monthly", 3.0),
            "total": _snapshot("total", 21.0),
        },
    )
    assert not decision.accept
    assert decision.period_breached == "total"


def test_first_breach_short_circuits():
    """Multiple periods at limit — first checked (daily) wins."""
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug=None,
        risk_snapshots={
            "daily": _snapshot("daily", 6.0),
            "weekly": _snapshot("weekly", 11.0),
        },
    )
    assert decision.period_breached == "daily"


def test_no_snapshots_means_pass():
    """Fresh deploy with empty risk_ledger → accept (no DD seen yet)."""
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug="btc-momentum",
        risk_snapshots={},
    )
    assert decision.accept


def test_dd_at_exact_cap_breaches():
    """5.0% with 5.0% cap should reject — >= comparison."""
    decision = evaluate(
        halt_active=False,
        strategy_halt_active=False,
        strategy_slug=None,
        risk_snapshots={"daily": _snapshot("daily", 5.0)},
    )
    assert not decision.accept


def test_halt_takes_priority_over_dd():
    decision = evaluate(
        halt_active=True,
        strategy_halt_active=False,
        strategy_slug=None,
        risk_snapshots={"daily": _snapshot("daily", 99.0)},
    )
    assert decision.reason == "system_halted"
