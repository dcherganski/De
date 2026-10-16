"""Runtime configuration, read from environment variables (see platform/.env.example).

Every threshold here is an experimental setting chosen to be cautious, not a value shown to be
profitable. Secrets are `SecretStr` so they never end up in reprs, logs or API responses.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- storage -------------------------------------------------------------------------
    database_url: str = "postgresql+psycopg://trader:trader_local_only@127.0.0.1:5432/trading"

    # --- mode ----------------------------------------------------------------------------
    # PAPER is the default. LIVE additionally needs allow_live_trading, an adapter whose live
    # trading is verified, and a confirmation in the dashboard; none of that is switched on here.
    mode: Literal["paper", "live"] = "paper"
    allow_live_trading: bool = False

    # --- market data ---------------------------------------------------------------------
    # Bybit is the main exchange; Kraken is a fallback price source. BYBIT_REGION picks the REST host:
    # "eu" = api.bybit.eu for EEA accounts (Bybit EU), "global" = api.bybit.com.
    quote_venue: Literal["bybit", "kraken", "fixture"] = "bybit"
    bybit_region: Literal["eu", "global"] = "eu"
    symbol: str = "BTC/EUR"
    fixture_path: str | None = None  # replay file when quote_venue == "fixture" (tests, demos)
    max_quote_age_s: int = 15
    max_candle_gap_s: int = 420  # a 5-minute candle older than this counts as missing
    quote_snapshot_every_s: int = 10

    # --- TypeSafe Jev (docs.typesafe.ai/api, checked 2026-10-09) --------------------------
    typesafe_api_key: SecretStr | None = None
    typesafe_base_url: str = "https://api.typesafe.ai"
    jev_model: str = "jev-latest"
    jev_timeout_s: float = 10.0
    jev_max_retries: int = 2
    jev_price_usd_per_mtok: Decimal = Decimal("0.042")  # docs.typesafe.ai/models: input tokens only

    # --- news ----------------------------------------------------------------------------
    news_provider: Literal["rss", "fixture", "none"] = "rss"
    rss_feeds: list[str] = Field(default_factory=lambda: list(DEFAULT_RSS_FEEDS))
    news_fixture_path: str | None = None
    news_poll_s: int = 120
    news_max_age_for_reeval_s: int = 1800
    news_reeval_min_interval_s: int = 600
    news_items_in_state: int = 8

    # --- FX for reporting costs in EUR ----------------------------------------------------
    # ECB daily reference rates; the fallback is used (and labelled) when the feed is unavailable.
    usd_per_eur_fallback: Decimal = Decimal("1.08")

    # --- portfolio -----------------------------------------------------------------------
    initial_btc_value_eur: Decimal = Decimal("200")
    initial_cash_eur: Decimal = Decimal("0")

    # --- paper broker assumptions (shown on the dashboard) --------------------------------
    # Taker fee as a fraction of notional. 0.25% is Bybit EU's non-VIP spot taker rate
    # (bybit.eu help centre "Bybit EU Spot Fees Explained", checked 2026-10-09).
    paper_fee_rate: Decimal = Decimal("0.0025")
    paper_slippage_bps: Decimal = Decimal("5")
    paper_min_order_base: Decimal = Decimal("0.00005")
    paper_base_step: Decimal = Decimal("0.00000001")
    paper_min_cost_quote: Decimal = Decimal("0.50")
    paper_partial_fills: bool = True
    paper_top_of_book_participation: Decimal = Decimal("0.5")  # share of displayed size we take

    # --- strategy ------------------------------------------------------------------------
    horizon_min: int = 30
    target_margin_pct: Decimal = Decimal("0.10")  # added on top of round-trip costs
    stop_pct: Decimal = Decimal("0.50")
    buy_choice_min: Decimal = Decimal("0.55")
    buy_close_above_min: Decimal = Decimal("0.55")
    sell_choice_min: Decimal = Decimal("0.55")
    sell_close_below_min: Decimal = Decimal("0.55")
    signal_ttl_s: int = 120
    take_profit_pct: Decimal | None = None  # None = use the event target X
    max_hold_min: int = 30

    # --- risk ----------------------------------------------------------------------------
    max_order_eur: Decimal = Decimal("100")
    max_exposure_eur: Decimal = Decimal("400")
    daily_loss_limit_eur: Decimal = Decimal("10")
    max_trades_per_day: int = 6
    min_seconds_between_trades: int = 900
    max_spread_pct: Decimal = Decimal("0.15")
    pause_blocks_exits: bool = True

    # --- costs ---------------------------------------------------------------------------
    infra_cost_eur_per_month: Decimal = Decimal("0")
    news_cost_eur_per_month: Decimal = Decimal("0")

    # --- security ------------------------------------------------------------------------
    secrets_master_key: SecretStr | None = None  # Fernet key for credentials at rest
    session_ttl_h: int = 12
    cookie_secure: bool = False  # set true behind HTTPS
    mcp_token: SecretStr | None = None

    @field_validator("typesafe_api_key", "secrets_master_key", "mcp_token", mode="before")
    @classmethod
    def _blank_is_unset(cls, v):
        # `TYPESAFE_API_KEY=` in .env means "no key", not "an empty key".
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v

    @field_validator("rss_feeds", mode="before")
    @classmethod
    def _split_feeds(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @property
    def base_asset(self) -> str:
        return self.symbol.split("/")[0]

    @property
    def quote_asset(self) -> str:
        return self.symbol.split("/")[1]

    def strategy_config(self) -> dict:
        """The settings that shape a Jev decision; hashed into `config_version`."""
        keys = (
            "jev_model horizon_min target_margin_pct stop_pct buy_choice_min buy_close_above_min "
            "sell_choice_min sell_close_below_min signal_ttl_s paper_fee_rate paper_slippage_bps "
            "max_spread_pct news_items_in_state"
        ).split()
        return {k: str(getattr(self, k)) for k in keys}

    def config_version(self, question_template_version: str) -> str:
        blob = json.dumps({"q": question_template_version, **self.strategy_config()}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]


@lru_cache
def get_settings() -> Settings:
    return Settings()
