"""Env-driven settings."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Connections
    redis_url: str
    aicore_db_url: str  # writeable role required

    # alphas:active consumer
    alphas_stream: str = "alphas:active"
    consumer_group: str = "oms-gateway"
    consumer_name: str = "oms-gateway-1"
    block_ms: int = 5_000
    batch_size: int = 50

    # Risk caps (% of account equity). Mirror policy.md & feedback_trading_size.md.
    daily_dd_pct_cap: float = 5.0
    weekly_dd_pct_cap: float = 10.0
    monthly_dd_pct_cap: float = 15.0
    total_dd_pct_cap: float = 20.0

    # Sizing — used when alpha doesn't pin a notional.
    paper_account_equity_usd: float = 10_000.0
    default_per_trade_pct: float = 1.0  # conservative fallback
    bucket_size_pct_max: dict[str, float] = {
        "fast-intraday": 0.5,
        "swing": 1.5,
        "conviction": 5.0,
        "poly-bet": 2.0,
        "hedge": 3.0,
    }
    # Per-symbol overrides for bucket sizing — wins over bucket_size_pct_max
    # when the alpha's asset matches. Format (env var):
    #   "BTC-USDT:fast-intraday=0.7,ETH-USDT:fast-intraday=0.4,NVDA:fast-intraday=0.3"
    # Asset matching is case-insensitive on the asset key. Empty = no overrides.
    bucket_size_overrides: str = ""

    # Phase 2.8 — per-position cap multiplier. A position can scale up to
    # `bucket_size_pct × bucket_position_cap_mult × equity` USD before
    # further entries on the same (strategy, asset) get rejected.
    # 5.0 ⇒ a fast-intraday position can hold 5 trades' worth before capping.
    # Trades in the *opposite* direction always pass — let positions close.
    bucket_position_cap_mult: float = 5.0

    # Phase 2.9 — concentration guards. Cap total open notional across
    # all positions sharing a *bucket* (e.g. poly-bet) or an *underlying
    # cluster* (e.g. all bitcoin-* poly markets, or all BTC-USDT venues).
    # Defends against the 6-sell-wings-on-BTC failure mode where a single
    # strategy fans out across correlated markets and ends up with the
    # bucket total many multiples of intended sizing.
    # Caps are % of paper_account_equity_usd. Empty / 0 disables the cap.
    bucket_total_exposure_pct_cap: dict[str, float] = {
        "fast-intraday": 15.0,
        "swing": 20.0,
        "conviction": 30.0,
        "poly-bet": 20.0,
        "hedge": 15.0,
    }
    # Default cluster cap (% of equity) applied to every (venue, underlying)
    # cluster — e.g. all open `poly:bitcoin` markets sum to ≤ this. Set 0 to
    # disable the cluster guard entirely. 8% gives plenty of room for the
    # threshold-ladder strategies while still bounding correlated exposure.
    cluster_exposure_pct_cap: float = 8.0

    # --- Phase 3.1 — per-strategy capital budget ---
    # Hard ceiling on (existing open exposure + proposed notional) per
    # strategy, in USD. Prevents any one strategy from over-allocating
    # the bank-roll. Especially important with 20+ active strategies
    # where buckets + clusters alone don't isolate spend per slug.
    #
    # default_strategy_budget_usd applies to every strategy not in the
    # overrides map. Set 0 to disable the default (only overridden
    # strategies are capped).
    #
    # overrides format: "slug=usd_cap,slug2=usd_cap2"
    # Example:
    #   POLY-SELL-WINGS=500,POLY-PREMARKET-TOP-TAKER=300,POLY-PUBLISHER-TAKER=200
    default_strategy_budget_usd: float = 200.0
    strategy_budget_overrides: str = ""

    # --- 2026-05-18 — per-strategy per-ORDER cap ---
    # Caps the notional of each individual order from a strategy, independent
    # of total-exposure budget. Lets the strategy accumulate budget via many
    # small orders that the adapter's hard ceiling would otherwise refuse.
    #
    # Critical for live-mode flips. Example:
    #   STRATEGY_ORDER_CAP_OVERRIDES=poly-publisher-taker-long=20
    # With strategy_budget=400 and order_cap=20, the strategy can open up to
    # 20 concurrent $20 positions (= $400 total exposure). Without this,
    # oms-gateway sizes the first intent to $400, adapter (cap=$20) refuses.
    #
    # Same format as strategy_budget_overrides: "slug=usd,slug2=usd".
    # Empty = no per-order cap (sizing follows budget + bucket caps only).
    strategy_order_cap_overrides: str = ""

    # --- Bankroll-aware sizing (2026-05-17) ---
    # When True, per-strategy budget + per-order notional caps SCALE with
    # realized PnL using the tier ladder in bankroll_aware_sizing.py.
    # Default OFF — preserves current static-cap behavior.
    #
    # Ladder design: $500 start → T0 caps ($200/$50) → T1 ($400/$100) at
    # +$500 realized → T2 ($600/$150) at +$1500 → ... → T6 ($5000/$1200)
    # at +$25k. Caps RATCHET (revert to lower tier if PnL drops).
    bankroll_aware_sizing_enabled: bool = False
    # Refresher loop cadence (writes to Redis every N seconds).
    bankroll_refresh_interval_sec: int = 60
    # Initial seed capital for bankroll math. Realized PnL is added to this.
    bankroll_seed_capital_usd: float = 500.0

    # Aggressive bankroll ladder — 2x trade size, 2x strategy budget per tier.
    # Operator flips True AFTER:
    #   - 60+ live closed trades validate edge
    #   - Realized Sharpe ≥ 1.5 over 30 days
    #   - Max single-day drawdown < 8% of bankroll
    aggressive_bankroll_ladder: bool = False

    # --- Kelly capital allocator (2026-05-17) ---
    # Per-strategy budget multiplier based on rolling-Sharpe-weighted
    # Kelly fraction. Applied on top of tier budget; clamps to [floor, ceiling]
    # to protect against bad estimates.
    kelly_allocator_enabled: bool = False
    kelly_refresh_interval_sec: int = 600
    kelly_discount: float = 0.25         # quarter Kelly
    kelly_floor_multiplier: float = 0.25
    kelly_ceiling_multiplier: float = 3.0

    # Halt keys (must match pa-agent + risk-watcher)
    halt_key: str = "system:halt"
    halt_strategy_prefix: str = "system:halt:strategy:"

    # ── BULLETPROOF STRATEGY WHITELIST (2026-05-19 emergency) ───────────────
    # CSV of strategy slugs ALLOWED to place live orders. Empty string = no
    # whitelist enforced (back-compat). When set, preflight HARD-REJECTS any
    # alpha whose strategy_slug is not in this list, BEFORE any other check.
    #
    # Why: budget caps + env _ENABLED flags both had failure modes that
    # allowed non-edge strategies to fire $240+ of orders in 9 seconds onto
    # pre-market token-launch markets we have ZERO edge on. The whitelist
    # is a single non-bypassable invariant: only strategies in this list
    # can ever reach the live order path, period.
    #
    # Adding a strategy here is a deliberate operator action. Default is
    # chainlink_lag only — the strategy with proven data-stream edge.
    #
    # When adding new strategies: extend the CSV like
    #   LIVE_STRATEGY_WHITELIST_CSV=poly-chainlink-lag,new-strategy-slug
    # NEVER clear this to empty in production — that would re-introduce the
    # all-strategies-can-fire failure mode.
    live_strategy_whitelist_csv: str = "poly-chainlink-lag"

    # --- 2026-05-19 — per-strategy paper mode (Option B) ---
    # Comma-separated slugs whose alphas should route through the adapter's
    # dry-run code path (simulated fills) regardless of the global dry-run
    # setting. Lets us validate a strategy end-to-end (fill, exit monitor,
    # SL/TP firing) without putting real capital at risk, while other
    # strategies continue to trade live.
    #
    # The flag flows alpha → intent.metadata.paper → dispatcher payload →
    # poly-adapter, where it forces the dry-run branch on /orders/place.
    # Risk-watcher inherits it via the C.1 metadata propagation, so paper
    # positions are visible (and the chainlink_exit_monitor uses paper=True
    # on the SELL leg when closing them).
    #
    # Default empty = nothing is in paper mode.
    # Example: PAPER_STRATEGY_SLUGS=poly-chainlink-lag
    paper_strategy_slugs: str = ""

    # --- 2026-05-20 — duplicate-position guard (Bug 3 fix) ---
    # By DEFAULT, any same-direction alpha for an asset where the same
    # strategy already has an open position is REJECTED with reason
    # 'duplicate_position_same_direction'. This prevents the strategy's
    # re-evaluation tick (when a market stays in the watchlist longer than
    # the strategy's cooldown) from scaling-in to a 2× / 3× position.
    #
    # Strategies that LEGITIMATELY scale in (e.g. an averaging perp strategy)
    # must be added here to opt out of the guard:
    #   ALLOW_SAME_DIRECTION_SCALE_IN_STRATEGIES_CSV=averaging-perp,grid-bot
    #
    # Background: 2026-05-19 saw chainlink_lag emit two BUY alphas for the
    # same btc-updown-5m market 77s apart (cooldown is 60s). positions_aggregator
    # correctly scaled them into one $20 position, breaching the operator's
    # $10/trade cap intent. The cap check `_would_breach_position_cap` only
    # rejects when would-be exceeds the BUCKET cap (much higher than $10),
    # so the bug slipped through. This guard catches it at the (strategy, asset)
    # level instead of relying on bucket caps to do double duty.
    allow_same_direction_scale_in_strategies_csv: str = ""

    # Cap-breach event stream — Phase 2.9. Whenever the bucket or cluster
    # concentration guard rejects an intent, we XADD the breach payload
    # here so pa-agent can forward to Telegram.
    cap_breaches_stream: str = "risk:cap_breaches"
    cap_breaches_stream_maxlen: int = 10_000

    # Health endpoint
    health_port: int = 8003

    # Metrics — Phase 2.9 Prometheus exposition + cluster-exposure gauges.
    metrics_refresh_interval_sec: int = 60

    # Logging
    log_level: str = "INFO"


settings = Settings()  # type: ignore[call-arg]
