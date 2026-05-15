"""Main router: alphas:active → preflight → oms_intents.

Uses XREADGROUP for at-least-once consumption. Idempotency at the DB
level via UNIQUE (strategy_id, idempotency_key), so a redelivery after a
crash before XACK is safely deduped.
"""
import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import structlog
from signals_contract.alpha import Alpha

from oms_gateway.db import db
from oms_gateway.lp_multiplier import fetch_multiplier as fetch_lp_multiplier
from oms_gateway.preflight import (
    ExistingPosition,
    cluster_for,
    cluster_sql_filter,
    evaluate,
)
from oms_gateway.redis_client import r
from oms_gateway.settings import settings
from oms_gateway.sizing import compute_notional, derive_side, derive_venue

log = structlog.get_logger(__name__)


async def ensure_consumer_group() -> None:
    try:
        await r().xgroup_create(
            settings.alphas_stream,
            settings.consumer_group,
            id="0",
            mkstream=True,
        )
        log.info(
            "consumer_group.created",
            stream=settings.alphas_stream,
            group=settings.consumer_group,
        )
    except Exception as exc:
        if "BUSYGROUP" in str(exc):
            return
        raise


def _resolve_strategy(alpha: Alpha) -> tuple[UUID | None, str | None]:
    """Pull strategy_id + slug out of alpha.metadata.

    Producers should set:
        metadata.strategy_id (UUID string)
        metadata.strategy_slug (str)

    If absent, returns (None, None) and the alpha is dropped with a warning.
    """
    md = alpha.metadata or {}
    sid = md.get("strategy_id")
    slug = md.get("strategy_slug")
    parsed_sid: UUID | None = None
    if isinstance(sid, str):
        try:
            parsed_sid = UUID(sid)
        except ValueError:
            parsed_sid = None
    return parsed_sid, slug if isinstance(slug, str) else None


async def _check_halts(strategy_slug: str | None) -> tuple[bool, bool]:
    """Returns (system_halt, strategy_halt)."""
    sys_h = await r().exists(settings.halt_key) > 0
    if strategy_slug is None:
        return bool(sys_h), False
    key = f"{settings.halt_strategy_prefix}{strategy_slug}"
    strat_h = await r().exists(key) > 0
    return bool(sys_h), bool(strat_h)


async def _route_one(alpha: Alpha) -> None:
    if alpha.direction == "watch":
        log.debug("alpha.watch_skipped", alpha_id=str(alpha.id))
        return

    if not alpha.is_live(now=datetime.now(UTC)):
        log.info(
            "alpha.expired",
            alpha_id=str(alpha.id),
            expires_at=alpha.expires_at.isoformat(),
        )
        return

    strategy_id, strategy_slug = _resolve_strategy(alpha)
    if strategy_id is None:
        log.warning("alpha.no_strategy_id", alpha_id=str(alpha.id))
        return

    sys_halt, strat_halt = await _check_halts(strategy_slug)
    snapshots = await db.latest_risk_snapshots(scope="total")
    bucket = await db.strategy_bucket(strategy_id)

    # Pre-compute the proposed notional so the Phase 2.8 per-position cap
    # check can evaluate whether this trade would push the existing
    # position past its bucket cap. Sizing is pure / cheap; recomputed in
    # the accept branch below as the source of truth.
    #
    # Phase 3.0 (2026-05-15) — Liquidity Pulse internal risk filter:
    # fetch the LP multiplier from local Redis and pass into sizing so a
    # widening-spread shock on the alpha's asset automatically scales the
    # order DOWN. LP-untracked assets (stocks, forex) get 1.0 = unchanged.
    # Failures default to 1.0 — never amplify on missing data.
    lp_mult = (
        1.0
        if alpha.direction == "flat"
        else await fetch_lp_multiplier(r(), alpha.asset)
    )
    # Phase 3.2 — fetch strategy_exposure BEFORE sizing so the notional
    # respects the per-strategy budget cap (sizing.py applies the cap).
    # Was: preflight rejected 100% of intents that sized past the cap;
    # now sizing pre-caps and preflight is a defensive backstop only.
    strategy_exposure_for_sizing = (
        0.0
        if (alpha.direction == "flat" or not strategy_slug)
        else await db.strategy_open_exposure_usd(strategy_slug)
    )
    proposed_notional = (
        None
        if alpha.direction == "flat"
        else compute_notional(
            bucket=bucket,
            alpha_metadata=alpha.metadata,
            confidence=alpha.confidence,
            asset=alpha.asset,
            lp_multiplier=lp_mult,
            strategy_slug=strategy_slug,
            strategy_open_exposure_usd=strategy_exposure_for_sizing,
        )
    )

    existing_pos_row = await db.existing_open_position(
        strategy_id=strategy_id, asset=alpha.asset,
    )
    existing_position = (
        ExistingPosition(
            qty=float(existing_pos_row["qty"]),
            side=existing_pos_row["side"],
            mark_price=(
                float(existing_pos_row["mark_price"])
                if existing_pos_row["mark_price"] is not None
                else None
            ),
            avg_entry_price=float(existing_pos_row["avg_entry_price"]),
        )
        if existing_pos_row
        else None
    )

    # Phase 2.9 — concentration: total open notional in this bucket and in
    # the underlying cluster (e.g. all `poly:bitcoin` markets together).
    venue = derive_venue(alpha.asset_class, alpha.asset)
    bucket_exposure = (
        await db.bucket_open_exposure_usd(bucket) if bucket else 0.0
    )
    cluster = cluster_for(venue, alpha.asset)
    cluster_filter = cluster_sql_filter(cluster)
    cluster_exposure = (
        await db.cluster_open_exposure_usd(
            venue=cluster_filter[0],
            like_pattern=cluster_filter[1],
            exact=cluster_filter[2],
        )
        if cluster_filter is not None
        else 0.0
    )

    # Phase 3.1 — per-strategy budget: reuse the exposure already fetched
    # for sizing (above). When direction=='flat' (no sizing path), fetch now
    # so the preflight strategy_halt + budget checks still see real exposure.
    strategy_exposure = (
        strategy_exposure_for_sizing
        if alpha.direction != "flat"
        else (
            await db.strategy_open_exposure_usd(strategy_slug)
            if strategy_slug else 0.0
        )
    )

    decision = evaluate(
        halt_active=sys_halt,
        strategy_halt_active=strat_halt,
        strategy_slug=strategy_slug,
        risk_snapshots=snapshots,
        existing_position=existing_position,
        alpha_direction=alpha.direction,
        proposed_notional_usd=proposed_notional,
        bucket=bucket,
        bucket_open_exposure_usd=bucket_exposure,
        cluster=cluster,
        cluster_open_exposure_usd=cluster_exposure,
        strategy_open_exposure_usd=strategy_exposure,
    )

    if decision.accept and alpha.direction == "flat":
        side = "close"
        notional = None
    elif decision.accept:
        side = derive_side(alpha.direction)
        notional = proposed_notional
    else:
        side = derive_side(alpha.direction) if alpha.direction != "flat" else "close"
        notional = None  # rejected: don't size

    idempotency_key = f"{strategy_slug or strategy_id}:{alpha.id}:entry"

    metadata = {
        "alpha_id": str(alpha.id),
        "alpha_confidence": alpha.confidence,
        "alpha_edge_bps": alpha.edge_bps,
        "alpha_reasoning": alpha.reasoning,
        "bucket": bucket,
        "cluster": cluster,
        "lp_multiplier": lp_mult,    # Phase 3.0 — for forensic auditing
        "preflight_decision": {
            "accept": decision.accept,
            "reason": decision.reason,
            "period_breached": decision.period_breached,
            "snapshot_used": decision.snapshot_used,
        },
        "contributing_sources": [
            {"source_id": s.source_id, "weight": s.weight}
            for s in alpha.contributing_sources
        ],
    }

    intent_id = await db.insert_intent(
        strategy_id=strategy_id,
        signal_id=None,
        idempotency_key=idempotency_key,
        venue=venue,
        asset=alpha.asset,
        side=side,
        order_type="market",
        notional_usd=notional,
        qty=None,
        status="queued" if decision.accept else "rejected",
        rejection_reason=decision.reason,
        metadata=metadata,
    )

    if intent_id is None:
        log.info(
            "intent.duplicate",
            alpha_id=str(alpha.id),
            idempotency_key=idempotency_key,
        )
    else:
        log.info(
            "intent.recorded",
            intent_id=str(intent_id),
            alpha_id=str(alpha.id),
            asset=alpha.asset,
            side=side,
            notional=notional,
            status="queued" if decision.accept else "rejected",
            rejection_reason=decision.reason,
            strategy_slug=strategy_slug,
        )

    # Concentration-cap breaches are operator-visible — XADD to the
    # cap-breach stream so pa-agent can forward to Telegram. We
    # deliberately exclude `position_cap_exceeded` (per-strategy, common
    # under healthy scaling) — only the cross-strategy breaches go here.
    if decision.reason in (
        "bucket_exposure_cap_exceeded",
        "cluster_exposure_cap_exceeded",
    ):
        try:
            await r().xadd(
                settings.cap_breaches_stream,
                {
                    "data": json.dumps({
                        "ts": datetime.now(UTC).isoformat(),
                        "reason": decision.reason,
                        "strategy_slug": strategy_slug or "",
                        "asset": alpha.asset,
                        "venue": venue,
                        "bucket": bucket,
                        "cluster": cluster,
                        "snapshot": decision.snapshot_used,
                        "alpha_id": str(alpha.id),
                    }),
                },
                maxlen=settings.cap_breaches_stream_maxlen,
                approximate=True,
            )
        except Exception:
            log.exception("cap_breach.xadd_failed", reason=decision.reason)


async def loop() -> None:
    await ensure_consumer_group()
    log.info(
        "router.starting",
        stream=settings.alphas_stream,
        group=settings.consumer_group,
    )

    while True:
        try:
            result = await r().xreadgroup(
                settings.consumer_group,
                settings.consumer_name,
                {settings.alphas_stream: ">"},
                count=settings.batch_size,
                block=settings.block_ms,
            )
        except Exception:
            log.exception("router.read_failed")
            await asyncio.sleep(5)
            continue

        if not result:
            continue

        for _stream_name, entries in result:
            ack_ids: list[str] = []
            for entry_id, fields in entries:
                raw = fields.get("data") or fields.get(b"data")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                if not raw:
                    log.warning("router.empty_entry", entry_id=entry_id)
                    ack_ids.append(entry_id)
                    continue
                try:
                    payload = json.loads(raw)
                    alpha = Alpha.model_validate(payload)
                except Exception:
                    log.exception("router.bad_payload", entry_id=entry_id, raw=raw[:200])
                    ack_ids.append(entry_id)
                    continue

                try:
                    await _route_one(alpha)
                    ack_ids.append(entry_id)
                except Exception:
                    log.exception("router.route_failed", entry_id=entry_id)
                    # leave un-acked → redelivered next round

            if ack_ids:
                try:
                    await r().xack(settings.alphas_stream, settings.consumer_group, *ack_ids)
                except Exception:
                    log.exception("router.ack_failed", ack_ids=ack_ids)
