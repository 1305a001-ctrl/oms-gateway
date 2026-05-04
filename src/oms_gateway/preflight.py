"""L0 pre-trade checks.

Decides accept/reject for an Alpha based on:
  1. system:halt key (manual L5 halt, kill-switch UI)
  2. per-strategy halt key (system:halt:strategy:<slug>)
  3. drawdown caps from latest risk_ledger snapshots
       (daily / weekly / monthly / total)

Pure function — takes parsed inputs, returns Decision dataclass. No I/O.
The router pulls the inputs (Redis keys + DB rows) and feeds them in,
which keeps preflight unit-testable without mocks.
"""
from dataclasses import dataclass, field
from typing import Any, Literal

from oms_gateway.settings import settings


@dataclass(frozen=True)
class Decision:
    accept: bool
    reason: str | None
    period_breached: Literal["daily", "weekly", "monthly", "total"] | None = None
    snapshot_used: dict[str, Any] = field(default_factory=dict)


def _check_dd_breach(
    period: Literal["daily", "weekly", "monthly", "total"],
    snapshots: dict[str, dict[str, Any]],
    cap_pct: float,
) -> Decision | None:
    """Return a rejecting Decision if the period's drawdown breaches cap, else None."""
    snap = snapshots.get(period)
    if snap is None:
        return None
    dd_pct = float(snap.get("drawdown_pct") or 0.0)
    if dd_pct >= cap_pct:
        return Decision(
            accept=False,
            reason=f"{period}_dd_breached",
            period_breached=period,
            snapshot_used={
                "period": period,
                "drawdown_pct": dd_pct,
                "cap_pct": cap_pct,
                "snapshot_at": snap.get("snapshot_at"),
            },
        )
    return None


def evaluate(
    *,
    halt_active: bool,
    strategy_halt_active: bool,
    strategy_slug: str | None,
    risk_snapshots: dict[str, dict[str, Any]],
) -> Decision:
    """Run all preflight checks. First failing check rejects.

    Args:
        halt_active: True if system:halt key is set.
        strategy_halt_active: True if system:halt:strategy:<slug> key is set.
        strategy_slug: For logging context only.
        risk_snapshots: {period: row} dict from db.latest_risk_snapshots(scope='total').

    Returns:
        Decision with accept=True if all checks pass, else first failing
        Decision with reason + (optional) period_breached + snapshot_used.
    """
    if halt_active:
        return Decision(accept=False, reason="system_halted")

    if strategy_halt_active:
        return Decision(
            accept=False,
            reason="strategy_halted",
            snapshot_used={"strategy_slug": strategy_slug},
        )

    for period, cap in (
        ("daily", settings.daily_dd_pct_cap),
        ("weekly", settings.weekly_dd_pct_cap),
        ("monthly", settings.monthly_dd_pct_cap),
        ("total", settings.total_dd_pct_cap),
    ):
        breach = _check_dd_breach(period, risk_snapshots, cap)
        if breach is not None:
            return breach

    return Decision(accept=True, reason=None)
