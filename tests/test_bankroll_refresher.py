"""Tests for bankroll_refresher PnL query — particularly the paper-purged
position exclusion added 2026-05-19.
"""
from __future__ import annotations

from oms_gateway.bankroll_refresher import TOTAL_REALIZED_PNL_SQL


def test_pnl_query_excludes_paper_purged():
    """The SQL must filter out positions with metadata.paper_purged=true.

    Without this, the paper-purge cleanup (closing pre-flip paper positions
    to zero) accidentally left them counting toward bankroll-aware PnL.
    Observed live 2026-05-19: $215k fake PnL would have triggered T6_top
    tier ($5k budget, $1200 order cap) if bankroll-aware were enabled.
    """
    sql_lower = TOTAL_REALIZED_PNL_SQL.lower()
    assert "paper_purged" in sql_lower, (
        "TOTAL_REALIZED_PNL_SQL must exclude paper_purged positions to avoid "
        "the $215k fake PnL bug observed on 2026-05-19"
    )
    assert "coalesce" in sql_lower
    assert "!= 'true'" in sql_lower


def test_pnl_query_still_filters_polymarket_only():
    """Polymarket scope filter must remain — bankroll ramp should be
    driven by the validated live lane, not Binance/Alpaca paper."""
    sql_lower = TOTAL_REALIZED_PNL_SQL.lower()
    assert "venue = 'polymarket'" in sql_lower
    assert "status = 'closed'" in sql_lower


def test_pnl_query_uses_double_precision_cast():
    """Numeric → float cast guards against pgsql Decimal type quirks."""
    sql_lower = TOTAL_REALIZED_PNL_SQL.lower()
    assert "double precision" in sql_lower
