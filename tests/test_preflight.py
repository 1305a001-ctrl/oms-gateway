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


# ─── Phase 3.1 — per-strategy capital budget ──────────────────────


def test_parse_strategy_caps_basic():
    from oms_gateway.preflight import _parse_strategy_caps
    assert _parse_strategy_caps("poly-sell-wings=500,poly-publisher-taker=200") == {
        "poly-sell-wings": 500.0,
        "poly-publisher-taker": 200.0,
    }


def test_parse_strategy_caps_handles_garbage():
    from oms_gateway.preflight import _parse_strategy_caps
    assert _parse_strategy_caps("") == {}
    assert _parse_strategy_caps("only-key=,empty-val") == {}
    assert _parse_strategy_caps("=100,valid=50") == {"valid": 50.0}
    assert _parse_strategy_caps("malformed,valid=50,k=NaN") == {"valid": 50.0}


def test_strategy_budget_default_cap_applied(monkeypatch):
    """Default cap rejects when (existing + proposed) exceeds it."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "default_strategy_budget_usd", 100.0)
    monkeypatch.setattr(s.settings, "strategy_budget_overrides", "")
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-sell-wings",
        risk_snapshots={},
        proposed_notional_usd=60.0,
        strategy_open_exposure_usd=50.0,
        alpha_direction="long",
    )
    assert not d.accept
    assert d.reason == "strategy_budget_breached"
    assert d.snapshot_used["strategy_budget_usd"] == 100.0
    assert d.snapshot_used["would_be_exposure_usd"] == 110.0


def test_strategy_budget_within_cap_passes(monkeypatch):
    """Under the cap → accept."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "default_strategy_budget_usd", 100.0)
    monkeypatch.setattr(s.settings, "strategy_budget_overrides", "")
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-sell-wings",
        risk_snapshots={},
        proposed_notional_usd=30.0,
        strategy_open_exposure_usd=50.0,
        alpha_direction="long",
    )
    assert d.accept


def test_strategy_budget_override_wins(monkeypatch):
    """Per-strategy override beats the default."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "default_strategy_budget_usd", 100.0)
    monkeypatch.setattr(
        s.settings, "strategy_budget_overrides",
        "poly-sell-wings=500,poly-publisher-taker=200",
    )
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-sell-wings",
        risk_snapshots={},
        proposed_notional_usd=60.0,
        strategy_open_exposure_usd=50.0,
        alpha_direction="long",
    )
    assert d.accept
    d2 = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-publisher-taker",
        risk_snapshots={},
        proposed_notional_usd=60.0,
        strategy_open_exposure_usd=150.0,
        alpha_direction="long",
    )
    assert not d2.accept
    assert d2.reason == "strategy_budget_breached"


def test_strategy_budget_zero_default_disables(monkeypatch):
    """default=0 + no override = budget check never rejects."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "default_strategy_budget_usd", 0.0)
    monkeypatch.setattr(s.settings, "strategy_budget_overrides", "")
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="any-strategy",
        risk_snapshots={},
        proposed_notional_usd=50.0,
        strategy_open_exposure_usd=200.0,
        alpha_direction="long",
    )
    # Even though strategy_open_exposure is large, budget=0 disables the check
    assert d.reason != "strategy_budget_breached"


def test_strategy_budget_skipped_when_no_slug(monkeypatch):
    """No slug → can't enforce per-strategy budget; budget check never rejects."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "default_strategy_budget_usd", 100.0)
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug=None,
        risk_snapshots={},
        proposed_notional_usd=50.0,
        strategy_open_exposure_usd=200.0,    # would breach if slug were set
        alpha_direction="long",
    )
    assert d.reason != "strategy_budget_breached"


def test_strategy_budget_skipped_when_flat(monkeypatch):
    """alpha.direction='flat' → budget check never rejects (close path)."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "default_strategy_budget_usd", 100.0)
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="any-strategy",
        risk_snapshots={},
        proposed_notional_usd=50.0,
        strategy_open_exposure_usd=200.0,
        alpha_direction="flat",
    )
    assert d.reason != "strategy_budget_breached"


# --- 2026-05-20 Bug 3 — duplicate same-direction position guard ----------


def _existing(side: str, qty: float = 20.0, entry: float = 0.5):
    """Build an ExistingPosition snapshot for testing."""
    from oms_gateway.preflight import ExistingPosition
    return ExistingPosition(
        qty=qty, side=side, mark_price=entry, avg_entry_price=entry,
    )


def test_duplicate_same_direction_rejected_by_default(monkeypatch):
    """THE BUG 3 FIX. Same strategy + same asset + same direction with an
    open position → reject as duplicate. Without this, chainlink_lag's
    re-emit tick scales $10 into $20."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "allow_same_direction_scale_in_strategies_csv", "")
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots={},
        existing_position=_existing("long"),
        alpha_direction="long",
        proposed_notional_usd=10.0,
    )
    assert not d.accept
    assert d.reason == "duplicate_position_same_direction"


def test_counter_direction_allowed_for_close(monkeypatch):
    """Counter-direction alpha = close/reverse; must always pass the
    duplicate guard (it's reducing, not adding)."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "allow_same_direction_scale_in_strategies_csv", "")
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots={},
        existing_position=_existing("long"),  # have long
        alpha_direction="short",              # closing
        proposed_notional_usd=10.0,
    )
    assert d.reason != "duplicate_position_same_direction"


def test_no_existing_position_passes(monkeypatch):
    """Fresh open with no existing position → no duplicate issue."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "allow_same_direction_scale_in_strategies_csv", "")
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots={},
        existing_position=None,
        alpha_direction="long",
        proposed_notional_usd=10.0,
    )
    assert d.reason != "duplicate_position_same_direction"


def test_opted_in_strategy_can_scale_in(monkeypatch):
    """If strategy is whitelisted for scale-in (e.g. averaging-perp),
    the duplicate guard must NOT reject — let bucket cap do its job."""
    from oms_gateway import settings as s
    monkeypatch.setattr(
        s.settings, "allow_same_direction_scale_in_strategies_csv",
        "averaging-perp,grid-bot",
    )
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="averaging-perp",
        risk_snapshots={},
        existing_position=_existing("long"),
        alpha_direction="long",
        proposed_notional_usd=10.0,
    )
    assert d.reason != "duplicate_position_same_direction"


def test_flat_direction_skips_guard(monkeypatch):
    """flat = close, always passes (reduces position)."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "allow_same_direction_scale_in_strategies_csv", "")
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots={},
        existing_position=_existing("long"),
        alpha_direction="flat",
    )
    assert d.reason != "duplicate_position_same_direction"


# ── 2026-05-20 — direction-disable guard (post Audit A) ─────────────────


def test_direction_disabled_rejects_matching_pair(monkeypatch):
    """When STRATEGY_DISABLE_DIRECTIONS_CSV contains 'poly-chainlink-lag:long',
    every long alpha for chainlink_lag must be rejected — this is the
    safety guard while the fair_yes vol estimator is being fixed.
    """
    from oms_gateway import settings as s
    monkeypatch.setattr(
        s.settings, "strategy_disable_directions_csv",
        "poly-chainlink-lag:long",
    )
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots={},
        existing_position=None,
        alpha_direction="long",
        proposed_notional_usd=10.0,
    )
    assert not d.accept
    assert d.reason == "direction_disabled_pre_calibration"


def test_direction_disable_doesnt_affect_other_strategies(monkeypatch):
    """A `poly-chainlink-lag:long` disable does NOT block other strategies'
    long alphas — chainlink_lag's vol estimate problem is its own."""
    from oms_gateway import settings as s
    monkeypatch.setattr(
        s.settings, "strategy_disable_directions_csv",
        "poly-chainlink-lag:long",
    )
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-target-taker",
        risk_snapshots={},
        existing_position=None,
        alpha_direction="long",
        proposed_notional_usd=10.0,
    )
    assert d.reason != "direction_disabled_pre_calibration"


def test_direction_disable_allows_opposite_direction(monkeypatch):
    """Disabling long doesn't disable short — BUY_NO must remain enabled
    since calibration analysis showed it exploits the bias favorably."""
    from oms_gateway import settings as s
    monkeypatch.setattr(
        s.settings, "strategy_disable_directions_csv",
        "poly-chainlink-lag:long",
    )
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots={},
        existing_position=None,
        alpha_direction="short",
        proposed_notional_usd=10.0,
    )
    assert d.reason != "direction_disabled_pre_calibration"


def test_direction_disable_csv_multiple_pairs(monkeypatch):
    """Several pairs in the CSV — all must be enforced independently.
    Test scenario: both chainlink_lag long AND target_taker short disabled.
    """
    from oms_gateway import settings as s
    monkeypatch.setattr(
        s.settings, "strategy_disable_directions_csv",
        "poly-chainlink-lag:long, poly-target-taker:short",
    )
    # chainlink_lag long: blocked
    d1 = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots={},
        existing_position=None, alpha_direction="long",
        proposed_notional_usd=10.0,
    )
    assert d1.reason == "direction_disabled_pre_calibration"
    # target_taker short: blocked
    d2 = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-target-taker",
        risk_snapshots={},
        existing_position=None, alpha_direction="short",
        proposed_notional_usd=10.0,
    )
    assert d2.reason == "direction_disabled_pre_calibration"


def test_direction_disable_flat_always_allowed(monkeypatch):
    """Closing positions (flat) must NEVER be blocked by the direction
    disable — we want to be able to close even on disabled directions."""
    from oms_gateway import settings as s
    monkeypatch.setattr(
        s.settings, "strategy_disable_directions_csv",
        "poly-chainlink-lag:long,poly-chainlink-lag:short",
    )
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots={},
        existing_position=_existing("long"),
        alpha_direction="flat",
        proposed_notional_usd=10.0,
    )
    assert d.reason != "direction_disabled_pre_calibration"


def test_direction_disable_empty_csv_does_nothing(monkeypatch):
    """Empty/unset CSV → no direction blocked. Default state."""
    from oms_gateway import settings as s
    monkeypatch.setattr(s.settings, "strategy_disable_directions_csv", "")
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots={},
        existing_position=None, alpha_direction="long",
        proposed_notional_usd=10.0,
    )
    assert d.reason != "direction_disabled_pre_calibration"


def test_direction_disable_malformed_pair_ignored(monkeypatch):
    """A malformed entry (missing ':') must not crash — just skip it.
    Defensive: a typo in env shouldn't break the gateway."""
    from oms_gateway import settings as s
    monkeypatch.setattr(
        s.settings, "strategy_disable_directions_csv",
        "garbage,poly-chainlink-lag:long",
    )
    d = evaluate(
        halt_active=False, strategy_halt_active=False,
        strategy_slug="poly-chainlink-lag",
        risk_snapshots={},
        existing_position=None, alpha_direction="long",
        proposed_notional_usd=10.0,
    )
    # The valid pair still blocks
    assert d.reason == "direction_disabled_pre_calibration"
