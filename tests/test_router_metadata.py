"""Intent-metadata construction tests.

The router copies alpha-level state into oms_intents.metadata so downstream
EV analyses can segment trades by edge_pp / spread_pp / etc. without
joining back to the (potentially expired) alphas stream. These tests pin
the contract — especially the alpha_edge_pp hoist that closes the
2026-05-19 "flying without instruments" gap.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from signals_contract.alpha import Alpha, ContributingSource

from oms_gateway.preflight import Decision
from oms_gateway.router import build_intent_metadata
from oms_gateway.settings import settings


def _alpha(metadata: dict | None = None, *, sources: list | None = None) -> Alpha:
    now = datetime.now(UTC)
    return Alpha(
        id=uuid4(),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        asset_class="predictions",
        asset="poly:chainlink-lag-btc",
        direction="long",
        confidence=0.8,
        edge_bps=42.0,
        contributing_sources=sources or [],
        reasoning="r",
        metadata=metadata or {},
    )


def _decision(accept: bool = True) -> Decision:
    return Decision(
        accept=accept,
        reason=None if accept else "test_reject",
        period_breached=None,
        snapshot_used={"daily_pnl": -10.0},
    )


def test_alpha_edge_pp_hoisted_from_metadata():
    """edge_pp from alpha.metadata MUST appear as alpha_edge_pp at top level —
    this is the 2026-05-19 instrumentation gap fix. Querying intent.metadata
    directly in SQL must not require a nested JSON path traversal."""
    md = build_intent_metadata(
        alpha=_alpha(metadata={"edge_pp": 0.25, "spread_pp": 0.10}),
        bucket="poly-fast",
        cluster="poly:bitcoin",
        lp_mult=1.0,
        decision=_decision(),
    )
    assert md["alpha_edge_pp"] == 0.25


def test_alpha_metadata_preserved_verbatim():
    """The full alpha.metadata bag is stored as alpha_metadata so future EV
    analyses can segment on any field the strategy emitted (spread_pp,
    fair_yes, current_oracle, time_left_s, etc.) without re-emitting code."""
    original = {
        "edge_pp": 0.25,
        "spread_pp": 0.10,
        "start_price": 0.55,
        "current_oracle": 50000.0,
        "fair_yes": 0.60,
        "time_left_s": 3600,
    }
    md = build_intent_metadata(
        alpha=_alpha(metadata=original),
        bucket="poly-fast",
        cluster="poly:bitcoin",
        lp_mult=1.0,
        decision=_decision(),
    )
    assert md["alpha_metadata"] == original


def test_alpha_metadata_is_isolated_copy():
    """Mutating the stored alpha_metadata MUST NOT mutate the original alpha
    object — defensive against accidental shared-reference bugs downstream."""
    original = {"edge_pp": 0.25}
    alpha = _alpha(metadata=original)
    md = build_intent_metadata(
        alpha=alpha,
        bucket="poly-fast",
        cluster="poly:bitcoin",
        lp_mult=1.0,
        decision=_decision(),
    )
    md["alpha_metadata"]["edge_pp"] = 99.0
    assert alpha.metadata["edge_pp"] == 0.25


def test_alpha_edge_pp_none_when_missing():
    """A strategy that doesn't emit edge_pp must leave alpha_edge_pp=None,
    not raise. Pre-chainlink-lag strategies (momentum etc.) don't carry it."""
    md = build_intent_metadata(
        alpha=_alpha(metadata={"spread_pp": 0.10}),
        bucket="poly-fast",
        cluster="poly:bitcoin",
        lp_mult=1.0,
        decision=_decision(),
    )
    assert md["alpha_edge_pp"] is None
    assert md["alpha_metadata"] == {"spread_pp": 0.10}


def test_empty_alpha_metadata_yields_empty_dict():
    md = build_intent_metadata(
        alpha=_alpha(metadata={}),
        bucket=None,
        cluster=None,
        lp_mult=1.0,
        decision=_decision(),
    )
    assert md["alpha_metadata"] == {}
    assert md["alpha_edge_pp"] is None


# ─── paper flag — per-strategy paper mode (2026-05-19 Option B) ─────────


def test_paper_flag_false_by_default():
    """Default settings = empty PAPER_STRATEGY_SLUGS → no strategy is in
    paper mode. Critical default: live trading is the path unless explicitly
    opted out."""
    md = build_intent_metadata(
        alpha=_alpha(metadata={"edge_pp": 0.25}),
        bucket="poly-fast",
        cluster="poly:bitcoin",
        lp_mult=1.0,
        decision=_decision(),
        strategy_slug="poly-chainlink-lag",
    )
    assert md["paper"] is False


def test_paper_flag_true_when_strategy_in_whitelist(monkeypatch):
    """Adding a strategy to PAPER_STRATEGY_SLUGS marks its intents paper=True."""
    monkeypatch.setattr(settings, "paper_strategy_slugs", "poly-chainlink-lag", raising=False)
    md = build_intent_metadata(
        alpha=_alpha(metadata={"edge_pp": 0.25}),
        bucket="poly-fast",
        cluster="poly:bitcoin",
        lp_mult=1.0,
        decision=_decision(),
        strategy_slug="poly-chainlink-lag",
    )
    assert md["paper"] is True


def test_paper_flag_isolated_per_strategy(monkeypatch):
    """Other strategies NOT in the whitelist remain live (paper=False)
    even when the whitelist is non-empty. This is the whole point — one
    strategy in paper, others continue trading real."""
    monkeypatch.setattr(settings, "paper_strategy_slugs", "poly-chainlink-lag", raising=False)
    md = build_intent_metadata(
        alpha=_alpha(metadata={"edge_pp": 0.25}),
        bucket="poly-fast",
        cluster="poly:bitcoin",
        lp_mult=1.0,
        decision=_decision(),
        strategy_slug="poly-publisher-taker-long",
    )
    assert md["paper"] is False


def test_paper_flag_handles_csv_with_whitespace(monkeypatch):
    """Operators often add slugs with stray spaces — must parse cleanly."""
    monkeypatch.setattr(
        settings, "paper_strategy_slugs",
        "  poly-foo , poly-chainlink-lag,  poly-bar  ",
        raising=False,
    )
    md = build_intent_metadata(
        alpha=_alpha(metadata={"edge_pp": 0.25}),
        bucket="poly-fast",
        cluster="poly:bitcoin",
        lp_mult=1.0,
        decision=_decision(),
        strategy_slug="poly-chainlink-lag",
    )
    assert md["paper"] is True


def test_paper_flag_false_when_strategy_slug_none(monkeypatch):
    """Defensive: if strategy_slug couldn't be resolved (None), don't paper-trade
    by accident — fall back to live (paper=False)."""
    monkeypatch.setattr(settings, "paper_strategy_slugs", "poly-chainlink-lag", raising=False)
    md = build_intent_metadata(
        alpha=_alpha(metadata={"edge_pp": 0.25}),
        bucket="poly-fast",
        cluster="poly:bitcoin",
        lp_mult=1.0,
        decision=_decision(),
        strategy_slug=None,
    )
    assert md["paper"] is False


def test_existing_fields_unchanged():
    """The refactor MUST preserve the pre-existing fields oms-dispatcher and
    forensic queries rely on. Regression guard."""
    src = ContributingSource(
        source_id="cross-market:chainlink-lag",
        source_kind="cross-market",
        weight=1.0,
        raw_confidence=0.9,
    )
    md = build_intent_metadata(
        alpha=_alpha(metadata={"edge_pp": 0.25}, sources=[src]),
        bucket="poly-fast",
        cluster="poly:bitcoin",
        lp_mult=0.85,
        decision=_decision(accept=False),
    )
    assert md["alpha_id"]
    assert md["alpha_confidence"] == 0.8
    assert md["alpha_edge_bps"] == 42.0
    assert md["alpha_reasoning"] == "r"
    assert md["bucket"] == "poly-fast"
    assert md["cluster"] == "poly:bitcoin"
    assert md["lp_multiplier"] == 0.85
    assert md["preflight_decision"]["accept"] is False
    assert md["preflight_decision"]["reason"] == "test_reject"
    assert md["contributing_sources"] == [
        {"source_id": "cross-market:chainlink-lag", "weight": 1.0}
    ]
