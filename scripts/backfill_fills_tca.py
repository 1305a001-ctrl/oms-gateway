#!/usr/bin/env python3
"""One-shot backfill for the `public.fills` TCA table (gap #3).

Context
-------
oms-gateway INSERTs the intent-side `public.fills` row at routing time
(commit 67a03e9), keyed by `parent_intent_id = oms_intents.id`. The venue
adapters now mirror the fill-side (fill_ts/ack_ts/fill_price/filled_size/
fee/fee_currency) on execution. But the `public.fills` table only began
being written recently, so the ~2k historical *filled* `oms_intents` rows
that predate it have NO `public.fills` row at all — their TCA history is
missing.

This script reconstructs those rows from `oms_intents`, which still holds
the fill data (fill_price, fill_qty, fees_usd, the intent/submit/complete
timestamps, and — for Polymarket — the arrival quote in metadata). It is:

  * idempotent  — only inserts where no fills row exists for the intent
                  (`WHERE NOT EXISTS`), so re-running is safe;
  * additive    — INSERT only, never UPDATE/DELETE of existing rows;
  * dry-run by default — prints the per-venue recovery plan and writes
                  NOTHING unless `--commit` is passed.

What it recovers (per filled oms_intents row)
---------------------------------------------
  intent_ts        ← created_at
  submit_ts        ← COALESCE(submitted_at, created_at)
  ack_ts           ← COALESCE(submitted_at, created_at)   [proxy — see below]
  fill_ts          ← COALESCE(completed_at, submitted_at, created_at)
  quote_at_intent  ← metadata #>> '{alpha_metadata,fair_yes}'  [Polymarket only]
  size             ← notional_usd
  filled_size      ← fill_qty
  fill_price       ← fill_price
  fee              ← fees_usd
  fee_currency     ← 'USDC' (polymarket) | 'USD' (alpaca/binance)
  outcome          ← 'filled'

Honest limitations (NOT faked)
------------------------------
  * ack_ts is a PROXY. `oms_intents` never stored a distinct broker-ack
    timestamp, so `submit_to_ack_secs` will read ~0 for backfilled rows.
    Forward fills (written live by the adapters) carry a real ack_ts.
    Backfilled rows are tagged `meta.tca_backfill = true` so analytics can
    exclude their latency splits if desired.
  * arrival_slip_bps is only recoverable where `quote_at_intent` exists —
    i.e. the subset of Polymarket rows whose alpha metadata carried
    `fair_yes`. Alpaca/binance history has no stored arrival quote, so
    their `arrival_slip_bps` stays NULL (the data was never captured —
    we do not invent a quote). Their LATENCY splits (intent→fill) DO
    backfill from the real timestamps.
  * gas_cost_usd stays NULL. None of these venues exposed a per-fill gas
    figure to the adapter at the time (CEX venues have none; Polymarket
    relayer gas was not captured per-intent).

Usage
-----
  # dry-run (default) — report only, no writes:
  python scripts/backfill_fills_tca.py

  # actually write:
  python scripts/backfill_fills_tca.py --commit

  # restrict to one venue:
  python scripts/backfill_fills_tca.py --venue polymarket --commit

Connection: reads AICORE_DB_URL from the same settings/.env the gateway uses,
or pass --db-url. NEVER run against live without an operator deciding to.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg

# Venue → fee_currency for reconstructed rows.
_FEE_CCY_BY_VENUE = {
    "polymarket": "USDC",
    "alpaca": "USD",
    "binance": "USD",
    "okx": "USD",
    "oanda": "USD",
}
_DEFAULT_FEE_CCY = "USD"

# Rows we can reconstruct: a terminal FILLED intent with real fill data that
# has no public.fills row yet. The LEFT JOIN ... IS NULL is the idempotency
# guard; metadata fair_yes is pulled for the arrival quote where present.
_SELECT_CANDIDATES = """
SELECT
    i.id                AS intent_id,
    i.strategy_id::text AS strategy_id,
    i.venue             AS venue,
    i.asset             AS asset,
    i.side              AS side,
    i.order_type        AS order_type,
    i.created_at        AS intent_ts,
    COALESCE(i.submitted_at, i.created_at)               AS submit_ts,
    COALESCE(i.completed_at, i.submitted_at, i.created_at) AS fill_ts,
    i.notional_usd      AS size,
    i.fill_qty          AS filled_size,
    i.fill_price        AS fill_price,
    i.fees_usd          AS fee,
    (i.metadata #>> '{alpha_metadata,fair_yes}')::numeric AS quote_at_intent
FROM oms_intents i
LEFT JOIN public.fills f ON f.parent_intent_id = i.id
WHERE i.status = 'filled'
  AND i.fill_price IS NOT NULL
  AND i.fill_qty IS NOT NULL AND i.fill_qty > 0
  AND f.fill_id IS NULL
  AND ($1::text IS NULL OR i.venue = $1::text)
"""

# Set-based INSERT mirroring the SELECT above. gen_random_uuid() for fill_id;
# ack_ts = submit_ts proxy; gas_cost_usd left NULL. meta tags the backfill so
# analytics can separate reconstructed rows from live-written ones.
_INSERT = """
INSERT INTO public.fills (
    fill_id, strategy_id, venue, asset, side,
    intent_ts, submit_ts, ack_ts, fill_ts,
    quote_at_intent, quote_at_submit, fill_price,
    size, filled_size, fee, fee_currency,
    order_type, outcome, parent_intent_id, meta
)
SELECT
    gen_random_uuid(),
    i.strategy_id::text,
    i.venue,
    i.asset,
    i.side,
    i.created_at,
    COALESCE(i.submitted_at, i.created_at),
    COALESCE(i.submitted_at, i.created_at),   -- ack_ts proxy
    COALESCE(i.completed_at, i.submitted_at, i.created_at),
    (i.metadata #>> '{alpha_metadata,fair_yes}')::numeric,
    (i.metadata #>> '{alpha_metadata,fair_yes}')::numeric,
    i.fill_price,
    i.notional_usd,
    i.fill_qty,
    i.fees_usd,
    CASE i.venue
        WHEN 'polymarket' THEN 'USDC'
        ELSE 'USD'
    END,
    i.order_type,
    'filled',
    i.id,
    jsonb_build_object(
        'tca_backfill', true,
        'tca_backfill_source', 'oms_intents',
        'ack_ts_is_proxy', true
    )
FROM oms_intents i
LEFT JOIN public.fills f ON f.parent_intent_id = i.id
WHERE i.status = 'filled'
  AND i.fill_price IS NOT NULL
  AND i.fill_qty IS NOT NULL AND i.fill_qty > 0
  AND f.fill_id IS NULL
  AND ($1::text IS NULL OR i.venue = $1::text)
"""


async def _report(conn: asyncpg.Connection, venue: str | None) -> int:
    """Print the per-venue recovery plan. Returns total candidate count."""
    # The only interpolated value is the module-level constant
    # _SELECT_CANDIDATES (a static query fragment, not user input); the venue
    # filter inside it is a real bind param ($1). No injection vector — S608
    # is a false positive on this CTE composition.
    report_sql = f"""
        WITH c AS ({_SELECT_CANDIDATES})
        SELECT
            venue,
            count(*)                                        AS recoverable,
            count(quote_at_intent)                          AS with_arrival_quote,
            count(*) FILTER (WHERE fill_ts > intent_ts)     AS with_latency
        FROM c
        GROUP BY venue
        ORDER BY recoverable DESC
    """  # noqa: S608
    rows = await conn.fetch(report_sql, venue)
    total = 0
    print("\nBackfill plan — historical filled intents with no public.fills row:")
    print(f"  {'venue':<12} {'recoverable':>11} {'arrival_slip':>13} {'latency':>9}")
    print("  " + "-" * 48)
    for r in rows:
        total += r["recoverable"]
        print(
            f"  {r['venue']:<12} {r['recoverable']:>11} "
            f"{r['with_arrival_quote']:>13} {r['with_latency']:>9}"
        )
    print("  " + "-" * 48)
    print(f"  {'TOTAL':<12} {total:>11}")
    print(
        "\n  arrival_slip column = rows with a recoverable quote_at_intent "
        "(arrival_slip_bps will compute).\n"
        "  latency column      = rows where fill_ts > intent_ts "
        "(intent_to_fill_secs will compute).\n"
        "  Backfilled rows are tagged meta.tca_backfill=true; ack_ts is a "
        "submit-time proxy (submit_to_ack_secs ~ 0).\n"
    )
    return total


async def run(*, db_url: str, venue: str | None, commit: bool) -> int:
    conn = await asyncpg.connect(db_url)
    try:
        total = await _report(conn, venue)
        if total == 0:
            print("Nothing to backfill — every filled intent already has a "
                  "fills row (or none qualify).")
            return 0
        if not commit:
            print("DRY-RUN — no rows written. Re-run with --commit to apply.")
            return 0

        # Wrap in a transaction so a mid-flight error rolls the whole batch
        # back — never leave the table half-backfilled.
        async with conn.transaction():
            result = await conn.execute(_INSERT, venue)
        # asyncpg returns 'INSERT 0 <n>'.
        written = int(result.split()[-1]) if result.startswith("INSERT") else 0
        print(f"COMMITTED — inserted {written} fills rows.")
        # Post-write sanity: how many now have computable TCA metrics.
        check = await conn.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE meta ->> 'tca_backfill' = 'true')
                    AS backfilled,
                count(*) FILTER (
                    WHERE meta ->> 'tca_backfill' = 'true'
                      AND arrival_slip_bps IS NOT NULL
                ) AS with_slip,
                count(*) FILTER (
                    WHERE meta ->> 'tca_backfill' = 'true'
                      AND intent_to_fill_secs IS NOT NULL
                ) AS with_latency
            FROM public.fill_tca_summary s
            JOIN public.fills fl ON fl.fill_id = s.fill_id
            """
        )
        if check is not None:
            print(
                f"  fill_tca_summary now shows {check['backfilled']} backfilled "
                f"rows: {check['with_slip']} with arrival_slip_bps, "
                f"{check['with_latency']} with intent_to_fill_secs."
            )
        return 0
    finally:
        await conn.close()


def _resolve_db_url(arg_url: str | None) -> str:
    if arg_url:
        return arg_url
    # Fall back to the gateway's own settings (reads AICORE_DB_URL / .env).
    try:
        from oms_gateway.settings import settings

        return settings.aicore_db_url
    except Exception as exc:  # noqa: BLE001
        print(
            f"error: no --db-url given and could not load settings ({exc}).\n"
            "Pass --db-url postgresql://user:pass@host/aicore",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--commit", action="store_true",
        help="actually write rows (default: dry-run report only)",
    )
    ap.add_argument(
        "--venue", default=None,
        help="restrict to one venue (polymarket/alpaca/binance/...)",
    )
    ap.add_argument(
        "--db-url", default=None,
        help="Postgres URL (default: gateway settings AICORE_DB_URL)",
    )
    args = ap.parse_args()
    db_url = _resolve_db_url(args.db_url)
    rc = asyncio.run(run(db_url=db_url, venue=args.venue, commit=args.commit))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
