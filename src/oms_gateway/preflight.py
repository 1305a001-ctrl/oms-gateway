"""L0 pre-trade checks.

Decides accept/reject for an Alpha based on:
  1. system:halt key (manual L5 halt, kill-switch UI)
  2. per-strategy halt key (system:halt:strategy:<slug>)
  3. drawdown caps from latest risk_ledger snapshots
       (daily / weekly / monthly / total)
  4. **per-position size cap** (Phase 2.8) — prevents runaway scaling
     when a strategy fires repeatedly on the same asset. Existing
     open-position size + would-be trade contribution must stay below
     bucket_size_pct_max[bucket] * bucket_position_cap_mult * equity.

Pure function — takes parsed inputs, returns Decision dataclass. No I/O.
The router pulls the inputs (Redis keys + DB rows + existing position
lookup) and feeds them in, which keeps preflight unit-testable.
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


@dataclass(frozen=True)
class ExistingPosition:
    """Snapshot of the open position (if any) for the alpha's (strategy, asset).

    Passed by the router after a single DB lookup. None ⇒ no open position
    (so the position-cap check just verifies the trade itself fits).
    """
    qty: float                    # signed against side, or always positive — see _would_be_qty
    side: Literal["long", "short"]
    mark_price: float | None      # may be None pre-MTM-tick; falls back to avg_entry
    avg_entry_price: float


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


def _resolve_position_cap_usd(bucket: str | None) -> float:
    """Per-bucket position cap = bucket_pct × position_cap_mult × equity. Pure."""
    bucket_pct = settings.bucket_size_pct_max.get(
        bucket or "", settings.default_per_trade_pct
    )
    return (
        bucket_pct / 100.0
        * settings.bucket_position_cap_mult
        * settings.paper_account_equity_usd
    )


def _would_breach_position_cap(
    *,
    existing: ExistingPosition | None,
    alpha_direction: Literal["long", "short", "flat"],
    proposed_notional_usd: float | None,
    bucket: str | None,
) -> Decision | None:
    """Pure: would the post-trade position size breach its bucket cap?

    Rules:
      - flat / close trades always pass (we want to be able to exit).
      - If alpha direction is opposite of existing position, treat as close
        / reverse — pass (the trade reduces or flips the position; the new
        leg will be re-checked when it accumulates).
      - If alpha direction matches existing side (or no existing position),
        compute would-be notional and compare against the per-bucket cap.

    Returns rejecting Decision on breach, else None.
    """
    if alpha_direction == "flat":
        return None
    if proposed_notional_usd is None or proposed_notional_usd <= 0:
        return None

    cap_usd = _resolve_position_cap_usd(bucket)
    if cap_usd <= 0:
        return None  # cap disabled

    if existing is not None and existing.side != alpha_direction:
        # opposite-side trade — reduces or reverses; let it through.
        return None

    # Mark price: prefer last MTM, fall back to existing entry, fall back to
    # the proposed-notional / 1 (so we at least cap on the dollar size).
    mark = (
        (existing.mark_price if existing and existing.mark_price else None)
        or (existing.avg_entry_price if existing else None)
    )

    if existing is None:
        # No prior position — would-be size = this trade only.
        would_be_notional = proposed_notional_usd
    elif mark is None or mark <= 0:
        # Defensive: if we have no usable mark, sum at proposed-notional level.
        would_be_notional = proposed_notional_usd + (existing.qty * existing.avg_entry_price)
    else:
        existing_notional_at_mark = existing.qty * mark
        would_be_notional = existing_notional_at_mark + proposed_notional_usd

    if would_be_notional > cap_usd:
        return Decision(
            accept=False,
            reason="position_cap_exceeded",
            snapshot_used={
                "bucket": bucket,
                "cap_usd": round(cap_usd, 2),
                "would_be_notional_usd": round(would_be_notional, 2),
                "existing_qty": existing.qty if existing else 0,
                "proposed_notional_usd": proposed_notional_usd,
            },
        )
    return None


def evaluate(
    *,
    halt_active: bool,
    strategy_halt_active: bool,
    strategy_slug: str | None,
    risk_snapshots: dict[str, dict[str, Any]],
    # Phase 2.8 — per-position cap inputs (optional for back-compat with
    # tests that don't care about position scaling).
    existing_position: ExistingPosition | None = None,
    alpha_direction: Literal["long", "short", "flat"] = "long",
    proposed_notional_usd: float | None = None,
    bucket: str | None = None,
) -> Decision:
    """Run all preflight checks. First failing check rejects.

    Args:
        halt_active: True if system:halt key is set.
        strategy_halt_active: True if system:halt:strategy:<slug> key is set.
        strategy_slug: For logging context only.
        risk_snapshots: {period: row} dict from db.latest_risk_snapshots(scope='total').
        existing_position: Snapshot of the open (strategy, asset) position
            if any. None = no open position. Used by per-position cap.
        alpha_direction: Alpha.direction. Determines whether this trade
            is opening / scaling / closing the existing position.
        proposed_notional_usd: Proposed trade size after sizing (post-
            confidence-scale). None = sizing didn't run (rejected upstream).
        bucket: Strategy bucket. Drives the per-position cap calculation.

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

    # Type annotation pins the period strings as literals so mypy can prove
    # they match _check_dd_breach's Literal accept set.
    dd_checks: tuple[tuple[Literal["daily", "weekly", "monthly", "total"], float], ...] = (
        ("daily", settings.daily_dd_pct_cap),
        ("weekly", settings.weekly_dd_pct_cap),
        ("monthly", settings.monthly_dd_pct_cap),
        ("total", settings.total_dd_pct_cap),
    )
    for period, cap in dd_checks:
        breach = _check_dd_breach(period, risk_snapshots, cap)
        if breach is not None:
            return breach

    pos_breach = _would_breach_position_cap(
        existing=existing_position,
        alpha_direction=alpha_direction,
        proposed_notional_usd=proposed_notional_usd,
        bucket=bucket,
    )
    if pos_breach is not None:
        return pos_breach

    return Decision(accept=True, reason=None)
