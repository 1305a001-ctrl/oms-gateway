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

    # Halt keys (must match pa-agent + risk-watcher)
    halt_key: str = "system:halt"
    halt_strategy_prefix: str = "system:halt:strategy:"

    # Health endpoint
    health_port: int = 8003

    # Logging
    log_level: str = "INFO"


settings = Settings()  # type: ignore[call-arg]
