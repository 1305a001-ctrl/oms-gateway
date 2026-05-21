"""Tests for the one-shot TCA backfill script (scripts/backfill_fills_tca.py).

Integration against a live Postgres is done manually by an operator running
the script with --commit. These tests guard the load-bearing logic that has
no business regressing silently:
  * the SELECT/INSERT carry the idempotency guard (LEFT JOIN ... IS NULL)
    so a re-run can't double-insert;
  * the venue filter is parameterised (not string-interpolated);
  * fee_currency maps polymarket→USDC, others→USD;
  * backfilled rows are tagged meta.tca_backfill so analytics can split them;
  * dry-run (default) writes NOTHING; --commit runs inside a transaction.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "backfill_fills_tca.py"


@pytest.fixture
def mod():
    if "backfill_fills_tca" in sys.modules:
        return sys.modules["backfill_fills_tca"]
    spec = importlib.util.spec_from_file_location("backfill_fills_tca", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_fills_tca"] = module
    spec.loader.exec_module(module)
    return module


# ── SQL structure ──────────────────────────────────────────────────────────


def test_insert_is_idempotent(mod):
    """Re-running must not double-insert: the INSERT...SELECT must exclude
    intents that already have a fills row."""
    sql = mod._INSERT
    assert "LEFT JOIN public.fills f ON f.parent_intent_id = i.id" in sql
    assert "f.fill_id IS NULL" in sql
    assert "INSERT INTO public.fills" in sql


def test_insert_only_filled_with_real_data(mod):
    sql = mod._INSERT
    assert "i.status = 'filled'" in sql
    assert "i.fill_price IS NOT NULL" in sql
    assert "i.fill_qty IS NOT NULL AND i.fill_qty > 0" in sql


def test_venue_filter_is_parameterised(mod):
    """Venue must be a bind param ($1), never string-interpolated (injection /
    correctness). Both report SELECT and INSERT honour it."""
    assert "($1::text IS NULL OR i.venue = $1::text)" in mod._INSERT
    assert "($1::text IS NULL OR i.venue = $1::text)" in mod._SELECT_CANDIDATES


def test_fee_currency_mapping(mod):
    sql = mod._INSERT
    assert "WHEN 'polymarket' THEN 'USDC'" in sql
    assert "ELSE 'USD'" in sql


def test_backfilled_rows_are_tagged(mod):
    sql = mod._INSERT
    assert "'tca_backfill', true" in sql
    assert "'ack_ts_is_proxy', true" in sql


def test_quote_recovered_from_metadata(mod):
    """Polymarket arrival quote comes from metadata.alpha_metadata.fair_yes."""
    assert "metadata #>> '{alpha_metadata,fair_yes}'" in mod._INSERT


def test_parent_intent_id_is_the_join_key(mod):
    """fills.parent_intent_id must be set to oms_intents.id so forward fills
    (written by adapters keyed on the same id) and backfilled rows agree."""
    # The INSERT's column list includes parent_intent_id and the SELECT emits i.id.
    assert "parent_intent_id" in mod._INSERT


# ── run() behaviour: dry-run vs commit ──────────────────────────────────────


def _fake_conn(*, total: int):
    """asyncpg connection double. _report() fetches grouped rows; run()'s
    commit path calls execute() + a final fetchrow()."""
    conn = MagicMock()
    group_rows = (
        [{
            "venue": "polymarket", "recoverable": total,
            "with_arrival_quote": total, "with_latency": total,
        }]
        if total
        else []
    )
    conn.fetch = AsyncMock(return_value=group_rows)
    conn.execute = AsyncMock(return_value=f"INSERT 0 {total}")
    conn.fetchrow = AsyncMock(return_value={
        "backfilled": total, "with_slip": total, "with_latency": total,
    })
    conn.close = AsyncMock()
    # async transaction() context manager
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return conn


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(mod, monkeypatch):
    conn = _fake_conn(total=42)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    rc = await mod.run(db_url="postgresql://x/y", venue=None, commit=False)
    assert rc == 0
    conn.execute.assert_not_called()  # dry-run: no INSERT
    conn.transaction.assert_not_called()


@pytest.mark.asyncio
async def test_commit_runs_insert_in_transaction(mod, monkeypatch):
    conn = _fake_conn(total=42)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    rc = await mod.run(db_url="postgresql://x/y", venue=None, commit=True)
    assert rc == 0
    conn.transaction.assert_called_once()  # batch wrapped in a tx
    conn.execute.assert_awaited_once()
    assert conn.execute.await_args.args[0] == mod._INSERT
    assert conn.execute.await_args.args[1] is None  # venue bind param


@pytest.mark.asyncio
async def test_commit_passes_venue_filter(mod, monkeypatch):
    conn = _fake_conn(total=5)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    await mod.run(db_url="postgresql://x/y", venue="polymarket", commit=True)
    assert conn.execute.await_args.args[1] == "polymarket"


@pytest.mark.asyncio
async def test_nothing_to_backfill_short_circuits(mod, monkeypatch):
    conn = _fake_conn(total=0)
    monkeypatch.setattr(mod.asyncpg, "connect", AsyncMock(return_value=conn))
    rc = await mod.run(db_url="postgresql://x/y", venue=None, commit=True)
    assert rc == 0
    conn.execute.assert_not_called()  # no candidates → no write even with --commit
