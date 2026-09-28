"""Static env config + runtime-tunable settings (persisted in the DB)."""
from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    host: str = "127.0.0.1"
    port: int = 8000
    dashboard_password: str = ""
    db_path: str = "vibetrader.db"

    wallet_private_key: str = ""
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    live_dry_run: bool = True

    jupiter_api_base: str = "https://lite-api.jup.ag"
    jupiter_api_key: str = ""


# Well-known Solana "majors" the bot also considers, alongside trending memecoins.
DEFAULT_WATCHLIST = [
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",  # JUP
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",  # WIF
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",  # JTO
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",  # RAY
]


class RuntimeSettings(BaseModel):
    """Everything the dashboard's SETTINGS.INI window can change."""

    # engine
    scan_interval_sec: int = Field(60, ge=10, le=3600)
    max_candidates_per_tick: int = Field(4, ge=1, le=20)
    decision_cooldown_min: int = Field(30, ge=1, le=1440)
    include_watchlist: bool = True
    watchlist: list[str] = Field(default_factory=lambda: list(DEFAULT_WATCHLIST))

    # candidate filters
    min_liquidity_usd: float = Field(20_000, ge=0)
    min_volume_h24_usd: float = Field(50_000, ge=0)
    min_age_min: float = Field(30, ge=0)
    max_rug_score: float = Field(50, ge=0, le=100)  # rugcheck score_normalised, higher = riskier

    # sizing / risk
    min_confidence: float = Field(0.6, ge=0, le=1)
    max_position_pct: float = Field(10, gt=0, le=100)
    max_open_positions: int = Field(5, ge=1, le=50)
    max_liquidity_share_pct: float = Field(1.0, gt=0, le=10)
    default_stop_loss_pct: float = Field(15, gt=0, le=90)
    default_take_profit_pct: float = Field(40, gt=0, le=1000)
    trailing_stop_pct: float = Field(12, ge=0, le=90)
    trailing_activation_pct: float = Field(15, ge=0, le=1000)
    max_hold_min: float = Field(240, ge=1)
    daily_loss_cap_usd: float = Field(0, ge=0)  # 0 = no cap in paper; required > 0 before live
    groq_bail_check: bool = True

    # paper fill model
    paper_base_slippage_pct: float = Field(0.5, ge=0, le=20)
    paper_fee_pct: float = Field(0.3, ge=0, le=5)
    paper_network_fee_usd: float = Field(0.05, ge=0)

    # live caps (all must be > 0 before live can be enabled)
    live_max_sol_per_trade: float = Field(0, ge=0)
    live_max_exposure_usd: float = Field(0, ge=0)
    live_slippage_bps: int = Field(300, ge=10, le=5000)


env = Env()
