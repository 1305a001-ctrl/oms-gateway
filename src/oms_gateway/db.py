"""Postgres connection pool + oms_intents writer + risk_ledger reader."""
import json
from datetime import datetime
from typing import Any
from uuid import UUID

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

    async def strategy_bucket(self, strategy_id: UUID) -> str | None:
        """Look up the bucket from strategies.frontmatter JSONB."""
        row = await self.pool.fetchrow(
            "SELECT frontmatter->>'bucket' AS bucket FROM strategies WHERE id = $1",
            strategy_id,
        )
        if row is None:
            return None
        return row["bucket"]

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


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, UUID):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


db = DB()
