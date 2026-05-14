"""Prometheus-format /metrics endpoint — Phase 2.9 cluster-exposure gauges.

Periodic task polls Postgres for current open notional grouped by bucket
and (venue, asset) → cluster, caches the values in memory. The /metrics
HTTP handler renders them into the Prometheus text exposition format.

We avoid the `prometheus_client` dependency — exposition format is small
+ stable, and skipping the dep keeps the container lean. If the gauge
set grows we can switch.

Gauges exposed:
  oms_bucket_open_exposure_usd{bucket="poly-bet"}                  current open notional
  oms_bucket_exposure_cap_usd{bucket="poly-bet"}                   configured cap
  oms_bucket_exposure_utilization{bucket="poly-bet"}               current/cap (0-1+)
  oms_cluster_open_exposure_usd{cluster="poly:bitcoin"}            current open notional
  oms_cluster_exposure_cap_usd                                     configured cap (single value)
  oms_cluster_exposure_utilization{cluster="poly:bitcoin"}         current/cap

The utilization gauge is the operator's "approach to cap" view — Grafana
can graph it and alert when > 0.8 long before the cap actually fires.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from aiohttp import web

from oms_gateway.db import db
from oms_gateway.preflight import cluster_for
from oms_gateway.settings import settings

log = logging.getLogger(__name__)


@dataclass
class _Snapshot:
    """In-memory cache of the latest exposure values."""
    by_bucket: dict[str, float] = field(default_factory=dict)
    by_cluster: dict[str, float] = field(default_factory=dict)
    last_refreshed_ts: float = 0.0


_snapshot = _Snapshot()


def _escape_label(value: str) -> str:
    """Pure: minimal Prometheus label-value escape."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(snap: _Snapshot) -> str:
    """Pure: Prometheus text exposition format from a snapshot.

    Stable output for testing — buckets + clusters sorted by name."""
    lines: list[str] = []

    # Bucket gauges
    cap_per_bucket: dict[str, float] = {
        b: pct / 100.0 * settings.paper_account_equity_usd
        for b, pct in settings.bucket_total_exposure_pct_cap.items()
        if pct > 0
    }
    lines.append("# HELP oms_bucket_open_exposure_usd Current open notional per bucket (USD)")
    lines.append("# TYPE oms_bucket_open_exposure_usd gauge")
    for bucket in sorted(snap.by_bucket):
        v = snap.by_bucket[bucket]
        lines.append(
            f'oms_bucket_open_exposure_usd{{bucket="{_escape_label(bucket)}"}} {v:.2f}'
        )

    lines.append("# HELP oms_bucket_exposure_cap_usd Configured per-bucket cap (USD)")
    lines.append("# TYPE oms_bucket_exposure_cap_usd gauge")
    for bucket in sorted(cap_per_bucket):
        cap = cap_per_bucket[bucket]
        lines.append(
            f'oms_bucket_exposure_cap_usd{{bucket="{_escape_label(bucket)}"}} {cap:.2f}'
        )

    lines.append("# HELP oms_bucket_exposure_utilization Current / cap; >1 means breach")
    lines.append("# TYPE oms_bucket_exposure_utilization gauge")
    for bucket in sorted(set(snap.by_bucket) | set(cap_per_bucket)):
        cap = cap_per_bucket.get(bucket, 0.0)
        cur = snap.by_bucket.get(bucket, 0.0)
        util = (cur / cap) if cap > 0 else 0.0
        lines.append(
            f'oms_bucket_exposure_utilization{{bucket="{_escape_label(bucket)}"}} {util:.4f}'
        )

    # Cluster gauges
    cluster_cap = settings.cluster_exposure_pct_cap / 100.0 * settings.paper_account_equity_usd
    lines.append(
        "# HELP oms_cluster_open_exposure_usd Current open notional per (venue, underlying) cluster"
    )
    lines.append("# TYPE oms_cluster_open_exposure_usd gauge")
    for cluster in sorted(snap.by_cluster):
        v = snap.by_cluster[cluster]
        lines.append(
            f'oms_cluster_open_exposure_usd{{cluster="{_escape_label(cluster)}"}} {v:.2f}'
        )

    lines.append("# HELP oms_cluster_exposure_cap_usd Configured per-cluster cap (USD)")
    lines.append("# TYPE oms_cluster_exposure_cap_usd gauge")
    lines.append(f"oms_cluster_exposure_cap_usd {cluster_cap:.2f}")

    lines.append("# HELP oms_cluster_exposure_utilization Current / cap; >1 means breach")
    lines.append("# TYPE oms_cluster_exposure_utilization gauge")
    for cluster in sorted(snap.by_cluster):
        v = snap.by_cluster[cluster]
        util = (v / cluster_cap) if cluster_cap > 0 else 0.0
        lines.append(
            f'oms_cluster_exposure_utilization{{cluster="{_escape_label(cluster)}"}} {util:.4f}'
        )

    return "\n".join(lines) + "\n"


async def metrics_handler(_request: web.Request) -> web.Response:
    body = render_metrics(_snapshot)
    return web.Response(
        text=body, content_type="text/plain; version=0.0.4; charset=utf-8",
    )


async def refresh_loop() -> None:
    """Background task — refresh the in-memory snapshot every N seconds."""
    while True:
        try:
            await _refresh_once()
        except Exception:
            log.exception("metrics.refresh_failed")
        await asyncio.sleep(settings.metrics_refresh_interval_sec)


async def _refresh_once() -> None:
    """One pass: query open positions, group by bucket + cluster, update cache."""
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
              s.frontmatter->>'bucket'                   AS bucket,
              p.venue, p.asset,
              p.qty * COALESCE(p.mark_price, p.avg_entry_price)::float AS notional
            FROM positions p
            JOIN strategies s ON s.id = p.strategy_id
            WHERE p.status = 'open' AND p.qty > 0
            """
        )

    by_bucket: dict[str, float] = {}
    by_cluster: dict[str, float] = {}
    for r in rows:
        bucket = r["bucket"]
        notional = float(r["notional"] or 0.0)
        if bucket:
            by_bucket[bucket] = by_bucket.get(bucket, 0.0) + notional
        cluster = cluster_for(r["venue"] or "", r["asset"] or "")
        by_cluster[cluster] = by_cluster.get(cluster, 0.0) + notional

    _snapshot.by_bucket = by_bucket
    _snapshot.by_cluster = by_cluster
    _snapshot.last_refreshed_ts = asyncio.get_event_loop().time()


# For tests
def _set_snapshot_for_test(by_bucket: dict[str, float], by_cluster: dict[str, float]) -> None:
    _snapshot.by_bucket = by_bucket
    _snapshot.by_cluster = by_cluster


__all__ = [
    "metrics_handler",
    "refresh_loop",
    "render_metrics",
]
