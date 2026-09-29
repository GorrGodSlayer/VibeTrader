"""Static env config + runtime-tunable settings (persisted in the DB)."""
from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    groq_api_key: str = ""
    # one model per job: each Groq model has its own daily quota, so splitting jobs multiplies the budget
    groq_model: str = "openai/gpt-oss-120b"             # entry decisions
    groq_screen_model: str = "qwen/qwen3.8-27b"         # picking which scanned tokens deserve a full analysis
    groq_manage_model: str = "openai/gpt-oss-20b"       # reviewing open positions
    groq_reflect_model: str = "qwen/qwen3.8-27b"        # post-mortems, strategy reviews, questions
    # per-model limits (Groq free tier); raise these on a paid tier
    groq_rpm: int = 30
    groq_tpm: int = 8000
    groq_tpd: int = 200_000

    host: str = "127.0.0.1"
    port: int = 8000
    dashboard_password: str = ""
    db_path: str = "vibetrader.db"

    wallet_private_key: str = ""
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    live_dry_run: bool = True

    jupiter_api_base: str = "https://lite-api.jup.ag"
    jupiter_api_key: str = ""

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        # The project's .env wins over process env vars, so a stale global GROQ_API_KEY can't shadow it.
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    @property
    def groq_models(self) -> dict[str, str]:
        return {"decide": self.groq_model, "screen": self.groq_screen_model,
                "manage": self.groq_manage_model, "reflect": self.groq_reflect_model}


# Well-known Solana "majors" the bot also considers, alongside trending memecoins.
DEFAULT_WATCHLIST = [
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",  # JUP
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",  # WIF
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",  # JTO
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",  # RAY
]


class RuntimeSettings(BaseModel):
    """Everything the Settings view can change. In autonomous mode the AI tunes the fields listed in
    vibetrader.brain.strategy.TUNABLE itself; everything else (hard backstops, live caps, fill model) is yours."""

    # autonomy
    autonomous: bool = True
    position_review_every_ticks: int = Field(1, ge=1, le=20)
    strategy_review_every_trades: int = Field(8, ge=1, le=200)
    strategy_review_max_hours: float = Field(12, ge=1, le=168)

    # engine
    scan_interval_sec: int = Field(300, ge=10, le=3600)
    max_candidates_per_tick: int = Field(2, ge=1, le=20)
    decision_cooldown_min: int = Field(30, ge=1, le=1440)
    include_watchlist: bool = True
    watchlist: list[str] = Field(default_factory=lambda: list(DEFAULT_WATCHLIST))

    # candidate filters
    min_liquidity_usd: float = Field(50_000, ge=0)
    min_volume_h24_usd: float = Field(50_000, ge=0)
    min_age_min: float = Field(30, ge=0)
    max_rug_score: float = Field(50, ge=0, le=100)  # rugcheck score_normalised, higher = riskier
    max_h1_change_pct: float = Field(100, ge=0, le=10_000)   # don't chase vertical candles
    min_h1_change_pct: float = Field(-30, ge=-100, le=0)     # don't catch falling knives
    max_entries_per_day: int = Field(12, ge=1, le=200)
    loss_streak_pause: int = Field(3, ge=0, le=20)           # consecutive losses that trigger a cool-down (0 = off)
    loss_streak_pause_min: float = Field(60, ge=0, le=1440)

    # sizing / risk
    min_confidence: float = Field(0.6, ge=0, le=1)
    # paper only: small "learning" trades on untested setups may skip the confidence bar
    explore_in_paper: bool = True
    explore_size_pct: float = Field(2, gt=0, le=10)
    explore_min_confidence: float = Field(0.25, ge=0, le=1)
    max_position_pct: float = Field(5, gt=0, le=100)
    max_open_positions: int = Field(5, ge=1, le=50)
    max_liquidity_share_pct: float = Field(1.0, gt=0, le=10)
    default_stop_loss_pct: float = Field(20, gt=0, le=90)
    default_take_profit_pct: float = Field(300, gt=0, le=1000)  # full exit; tranches below take profit on the way
    # scale out: sell a fraction of the ORIGINAL position at each level; after the first, the stop locks in profit
    scale_out_1_pct: float = Field(50, ge=0, le=1000)
    scale_out_1_frac: float = Field(0.4, ge=0, le=1)
    scale_out_2_pct: float = Field(150, ge=0, le=2000)
    scale_out_2_frac: float = Field(0.3, ge=0, le=1)
    scale_out_stop_lock_pct: float = Field(3, ge=0, le=50)
    # the runner: trail below the highest price once the trade is well up
    trailing_stop_pct: float = Field(25, ge=0, le=90)
    trailing_activation_pct: float = Field(50, ge=0, le=1000)
    # round-trip guard: once the peak gain reaches breakeven_after_pct, exit if it falls back to breakeven_lock_pct
    breakeven_after_pct: float = Field(30, ge=0, le=500)
    breakeven_lock_pct: float = Field(5, ge=-20, le=50)
    # profit floor: once peak gain reaches profit_floor_after_pct, keep at least profit_floor_keep of the peak gain
    profit_floor_after_pct: float = Field(50, ge=0, le=1000)
    profit_floor_keep: float = Field(0.5, ge=0, le=0.95)
    # time stop: exit if the trade hasn't gained time_stop_min_gain_pct in time (young pairs vs pairs older than 7 days)
    time_stop_min: float = Field(90, ge=0, le=2880)
    time_stop_min_gain_pct: float = Field(10, ge=-50, le=100)
    time_stop_mature_hours: float = Field(24, ge=0, le=168)
    # sell if pool liquidity falls this far below what the price move alone explains (liquidity pulled)
    liquidity_drain_exit_pct: float = Field(30, ge=0, le=90)
    max_hold_min: float = Field(1440, ge=1)
    exit_check_sec: int = Field(20, ge=5, le=300)  # price-only exit checks between AI ticks (no Groq cost)
    daily_loss_cap_usd: float = Field(0, ge=0)  # 0 = no cap in paper; required > 0 before live
    groq_bail_check: bool = True

    # hard backstops: never tuned by the AI, applied even when the AI says hold
    hard_stop_loss_pct: float = Field(40, gt=0, le=95)
    hard_max_hold_hours: float = Field(48, gt=0, le=720)

    # paper fill model
    paper_base_slippage_pct: float = Field(1.0, ge=0, le=20)  # research: ~1-3% per side for memecoins
    paper_fee_pct: float = Field(0.5, ge=0, le=5)
    paper_network_fee_usd: float = Field(0.05, ge=0)

    # live caps (all must be > 0 before live can be enabled)
    live_max_sol_per_trade: float = Field(0, ge=0)
    live_max_exposure_usd: float = Field(0, ge=0)
    live_slippage_bps: int = Field(300, ge=10, le=5000)


env = Env()
