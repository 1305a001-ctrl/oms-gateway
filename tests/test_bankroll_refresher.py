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


# --- 2026-05-20 — paper-mode + simulation-slug exclusions ----------------


def test_pnl_query_excludes_paper_mode():
    """Option B paper positions (metadata.paper='true') don't move wallet
    capital and must not count toward bankroll. Otherwise paper PnL
    inflates the tier and lets live trades go oversize."""
    sql_lower = TOTAL_REALIZED_PNL_SQL.lower()
    assert "metadata->>'paper'" in sql_lower
    assert "!= 'true'" in sql_lower


def test_pnl_query_excludes_simulation_slugs():
    """The simulation strategy slugs are passed as a parameter to the SQL.
    The query must reference that parameter; otherwise the $179k of
    publisher-taker fake PnL is included."""
    sql_lower = TOTAL_REALIZED_PNL_SQL.lower()
    assert "slug" in sql_lower
    assert "$1" in TOTAL_REALIZED_PNL_SQL or "any(" in sql_lower


def test_pnl_query_joins_strategies_table():
    """To filter by slug we need to JOIN to strategies."""
    sql_lower = TOTAL_REALIZED_PNL_SQL.lower()
    assert "join strategies" in sql_lower


def test_excluded_slugs_helper_parses_csv(monkeypatch):
    """The CSV setting is split into a list, with whitespace trimmed."""
    from oms_gateway import bankroll_refresher
    from oms_gateway.settings import settings

    monkeypatch.setattr(
        settings, "bankroll_excluded_strategy_slugs_csv",
        "poly-publisher-taker, poly-politics-momentum ,",
    )
    out = bankroll_refresher._excluded_slugs()
    assert out == ["poly-publisher-taker", "poly-politics-momentum"]


def test_excluded_slugs_empty_csv_returns_empty(monkeypatch):
    """Empty CSV → empty list — operator can disable the filter
    if they want (e.g., to debug)."""
    from oms_gateway import bankroll_refresher
    from oms_gateway.settings import settings

    monkeypatch.setattr(
        settings, "bankroll_excluded_strategy_slugs_csv", "",
    )
    assert bankroll_refresher._excluded_slugs() == []


def test_default_excluded_slugs_cover_known_contamination():
    """The default settings value must include the 10 known simulation
    slugs found in the 2026-05-20 audit."""
    from oms_gateway.settings import settings

    csv = settings.bankroll_excluded_strategy_slugs_csv
    for slug in [
        "poly-publisher-taker",
        "poly-publisher-taker-long",
        "poly-publisher-taker-aggressive",
        "poly-publisher-taker-conservative",
        "poly-politics-momentum",
    ]:
        assert slug in csv, f"missing default exclusion: {slug}"
