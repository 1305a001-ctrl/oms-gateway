"""Tiny aiohttp /health endpoint for uptime-kuma + grafana."""
import asyncio

import structlog
from aiohttp import web

from oms_gateway.db import db
from oms_gateway.redis_client import r
from oms_gateway.settings import settings

log = structlog.get_logger(__name__)


async def _health(_request: web.Request) -> web.Response:
    checks: dict[str, str] = {}

    try:
        async with db.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"down: {exc}"

    try:
        await r().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"down: {exc}"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    status = 200 if overall == "ok" else 503
    return web.json_response({"status": overall, "checks": checks}, status=status)


async def serve() -> None:
    app = web.Application()
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.health_port)  # noqa: S104
    await site.start()
    log.info("health.listening", port=settings.health_port)
    await asyncio.Event().wait()
