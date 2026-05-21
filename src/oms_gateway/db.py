"""Postgres connection pool + oms_intents writer + risk_ledger reader."""
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import structlog

from oms_gateway.settings import settings

log = structlog.get_logger(__name__)


class DB:
    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("DB not connected — call connect() first")
        return self._pool

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            settings.aicore_db_url,
            min_size=1,
            max_size=4,
        )
        log.info("db.connected", url=settings.aicore_db_url.split("@")[-1])

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            log.info("db.closed")

    async def latest_risk_snapshots(self, scope: str = "total") -> dict[str, dict[str, Any]]:
        """Latest risk_ledger row per period for one scope.

        Returns {period: row_dict}. Periods missing from DB are absent from
        the result. Empty {} if no snapshots exist yet (fresh deploy).
        """
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT ON (period)
                period, snapshot_at, pnl_usd, pnl_pct,
                drawdown_usd, drawdown_pct, high_water_mark_usd,
                exposure_usd
            FROM risk_ledger
            WHERE scope = $1
            ORDER BY period, snapshot_at DESC
            """,
            scope,
        )
        return {r["period"]: dict(r) for r in rows}

    async def existing_open_position(
        self, *, strategy_id: UUID, asset: str
    ) -> dict[str, Any] | None:
        """Return the open position for this (strategy, asset) if any.

        Used by Phase 2.8 per-position cap preflight check. Returns the
        most-recently-opened position when (rare) duplicates exist.
        """
        row = await self.pool.fetchrow(
            """
            SELECT qty, side, mark_price, avg_entry_price
            FROM positions
            WHERE strategy_id = $1 AND asset = $2 AND status = 'open'
              AND qty > 0
            ORDER BY opened_at DESC
            LIMIT 1
            """,
            strategy_id,
            asset,
        )
        return dict(row) if row else None

    async def strategy_bucket(self, strategy_id: UUID) -> str | None:
        """Look up the bucket from strategies.frontmatter JSONB."""
        row = await self.pool.fetchrow(
            "SELECT frontmatter->>'bucket' AS bucket FROM strategies WHERE id = $1",
            strategy_id,
        )
        if row is None:
            return None
        return row["bucket"]

    async def strategy_open_exposure_usd(self, strategy_slug: str) -> float:
        """Sum mark-priced open notional for a single strategy slug.

        Falls back to avg_entry_price when mark_price is NULL.
        Returns 0.0 when the strategy has no open positions.
        """
        if not strategy_slug:
            return 0.0
        row = await self.pool.fetchrow(
            """
            SELECT COALESCE(SUM(
              p.qty * COALESCE(p.mark_price, p.avg_entry_price)
            ), 0)::float AS exposure
            FROM positions p
            JOIN strategies s ON s.id = p.strategy_id
            WHERE s.slug = $1
              AND p.status = 'open' AND p.qty > 0
            """,
            strategy_slug,
        )
        return float(row["exposure"]) if row else 0.0

    async def bucket_open_exposure_usd(self, bucket: str) -> float:
        """Sum mark-priced open notional across all strategies sharing
        `frontmatter->>'bucket' = $1`. Falls back to avg_entry_price when
        mark_price is NULL (fresh open, MTM hasn't ticked yet).
        Returns 0.0 when bucket is empty/unknown."""
        if not bucket:
            return 0.0
        row = await self.pool.fetchrow(
            """
            SELECT COALESCE(SUM(
              p.qty * COALESCE(p.mark_price, p.avg_entry_price)
            ), 0)::float AS exposure
            FROM positions p
            JOIN strategies s ON s.id = p.strategy_id
            WHERE s.frontmatter->>'bucket' = $1
              AND p.status = 'open' AND p.qty > 0
            """,
            bucket,
        )
        return float(row["exposure"]) if row else 0.0

    async def cluster_open_exposure_usd(
        self, *, venue: str, like_pattern: str, exact: str,
    ) -> float:
        """Sum mark-priced open notional for (venue, asset matches).

        Caller derives `(venue, like_pattern, exact)` from a cluster key
        via `preflight.cluster_sql_filter()`. Falsy `like_pattern` and
        `exact` short-circuit to 0.0.
        """
        if not like_pattern and not exact:
            return 0.0
        row = await self.pool.fetchrow(
            """
            SELECT COALESCE(SUM(
              p.qty * COALESCE(p.mark_price, p.avg_entry_price)
            ), 0)::float AS exposure
            FROM positions p
            WHERE p.venue = $1
              AND (p.asset LIKE $2 OR p.asset = $3)
              AND p.status = 'open' AND p.qty > 0
            """,
            venue,
            like_pattern or "_NEVER_",
            exact or "_NEVER_",
        )
        return float(row["exposure"]) if row else 0.0

    async def insert_intent(
        self,
        *,
        strategy_id: UUID,
        signal_id: UUID | None,
        idempotency_key: str,
        venue: str,
        asset: str,
        side: str,
        order_type: str,
        notional_usd: float | None,
        qty: float | None,
        status: str,
        rejection_reason: str | None,
        metadata: dict,
    ) -> UUID | None:
        """Insert one oms_intents row. Returns id, or None on conflict (already exists)."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO oms_intents
              (strategy_id, signal_id, idempotency_key, venue, asset, side,
               order_type, notional_usd, qty, status, rejection_reason, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
            ON CONFLICT (strategy_id, idempotency_key) DO NOTHING
            RETURNING id
            """,
            strategy_id,
            signal_id,
            idempotency_key,
            venue,
            asset,
            side,
            order_type,
            notional_usd,
            qty,
            status,
            rejection_reason,
            json.dumps(metadata, default=_json_default),
        )
        return row["id"] if row else None

    async def record_intent_fill(
        self,
        *,
        intent_id: UUID | None,
        strategy_id: UUID,
        venue: str,
        asset: str,
        side: str,
        order_type: str,
        notional_usd: float | None,
        outcome: str,
        rejection_reason: str | None,
        quote_at_intent: float | None = None,
        meta: dict | None = None,
    ) -> None:
        """Best-effort intent-side write to the `fills` TCA table.

        NEVER raises — a fills-write failure must not affect order routing
        (wrapped in try/except, logged at warning). Captures the intent side:
        strategy/venue/asset/side/size/outcome/reject_reason + intent/submit
        timestamps. The price + fill fields (fill_price, fill_ts, fee, ack_ts)
        are backfilled by the venue adapter on execution — a separate change.
        """
        now = datetime.now(timezone.utc)
        try:
            await self.pool.execute(
                """
                INSERT INTO fills
                  (fill_id, strategy_id, venue, asset, side, intent_ts, submit_ts,
                   quote_at_intent, quote_at_submit, size, order_type, outcome,
                   reject_reason, parent_intent_id, meta)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb)
                """,
                uuid4(),
                str(strategy_id),
                venue,
                asset,
                side,
                now,
                now,
                quote_at_intent,
                quote_at_intent,
                notional_usd,
                order_type,
                outcome,
                rejection_reason,
                intent_id,
                json.dumps(meta or {}, default=_json_default),
            )
        except Exception as e:  # noqa: BLE001 — never let TCA break routing
            log.warning("fills.write_failed", error=str(e), intent_id=str(intent_id))


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, UUID):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


db = DB()
