# oms-gateway

**Phase 2 order-management gateway.** Reads `alphas:active` Redis Stream,
runs L0 preflight (drawdown caps + halt state), records intents in
`oms_intents` (postgres). Paper-mode in v0.1.0 — does not dispatch to
broker adapters yet.

## Architecture

```
news-consolidator + cross-market correlator
            │ Signal events
            ▼
   alpha-fusion layer (Phase 2)
            │ Alpha events
            ▼
        alphas:active  ◄── Redis Stream
            │
            ▼
      ╔══════════════╗      ┌────────────────────┐
      ║  oms-gateway ║─────►│  oms_intents       │
      ╚══════════════╝      │  (postgres)        │
            │                └────────────────────┘
            │ accepted intents (Phase 2.5)
            ▼
   per-bucket executor → trading-agent / poly-agent / forex-agent
```

## Preflight checks (L0)

In order — first failing check rejects:

1. `system:halt` Redis key is set → `system_halted`
2. `system:halt:strategy:<slug>` is set → `strategy_halted`
3. Latest `risk_ledger.daily.drawdown_pct` ≥ 5% → `daily_dd_breached`
4. Latest `risk_ledger.weekly.drawdown_pct` ≥ 10% → `weekly_dd_breached`
5. Latest `risk_ledger.monthly.drawdown_pct` ≥ 15% → `monthly_dd_breached`
6. Latest `risk_ledger.total.drawdown_pct` ≥ 20% → `total_dd_breached`

Caps are configurable via env (`*_DD_PCT_CAP`). Mirror policy.md.

## Sizing

Per-bucket caps (% of `paper_account_equity_usd`, default $10k):

| Bucket | Max % | $ on $10k |
|--------|-------|-----------|
| fast-intraday | 0.5% | $50 |
| swing | 1.5% | $150 |
| conviction | 5.0% | $500 |
| poly-bet | 2.0% | $200 |
| hedge | 3.0% | $300 |

If `alpha.metadata.suggested_notional_usd` is present, the lesser of
(alpha hint, bucket cap) is used. Then scaled by `alpha.confidence`,
floored at 25% to avoid dust trades.

## Idempotency

DB-level via `UNIQUE (strategy_id, idempotency_key)`. The router computes
`idempotency_key = "<strategy_slug>:<alpha.id>:entry"` so a redelivered
alpha (after a crash before XACK) will fail the unique constraint and be
treated as a duplicate.

## Run

```bash
pip install -e '.[dev]'
ruff check src/ tests/
pytest -q
oms-gateway   # daemon
```

Required env (see `src/oms_gateway/settings.py` for full list):

- `REDIS_URL`
- `AICORE_DB_URL` (writeable role)

## Health

`GET http://localhost:8002/health`
