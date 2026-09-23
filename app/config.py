from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mode: Literal["paper", "testnet", "live"] = "paper"
    allow_live_trading: bool = False

    symbol: str = "BTCUSDT"
    base_asset: str = "BTC"
    quote_asset: str = "USDT"
    interval: str = "15m"

    # Fast market/risk loop. Strategy still uses completed candles only.
    cycle_seconds: float = Field(default=1.0, ge=0.5, le=60)
    use_websocket_market_data: bool = True
    market_data_stale_seconds: float = Field(default=3.0, ge=1.0, le=30.0)
    strategy_refresh_grace_seconds: float = Field(default=1.0, ge=0.0, le=10.0)
    account_refresh_seconds: float = Field(default=5.0, ge=1.0, le=60.0)

    binance_api_key: str = ""
    binance_api_secret: str = ""

    paper_starting_balance: float = Field(default=1000.0, gt=0)
    risk_per_trade: float = Field(default=0.0025, gt=0, le=0.02)
    max_position_fraction: float = Field(default=0.05, gt=0, le=0.50)
    max_daily_loss_fraction: float = Field(default=0.01, gt=0, le=0.10)
    stop_loss_pct: float = Field(default=0.012, gt=0, le=0.10)
    take_profit_pct: float = Field(default=0.024, gt=0, le=0.30)
    min_signal_confidence: float = Field(default=0.70, ge=0.50, le=0.95)
    trading_fee_bps: float = Field(default=10.0, ge=0, le=100)
    paper_slippage_bps: float = Field(default=2.0, ge=0, le=100)

    enable_adaptive_learning: bool = True
    allow_adaptive_live: bool = False
    min_trades_for_learning: int = Field(default=50, ge=10, le=500)

    enable_llm_advisor: bool = False
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6"

    database_path: str = "data/trading_bot.db"
    learning_profile_path: str = "data/learning_profile.json"

    @model_validator(mode="after")
    def validate_runtime_settings(self):
        self.symbol = self.symbol.strip().upper()
        self.base_asset = self.base_asset.strip().upper()
        self.quote_asset = self.quote_asset.strip().upper()
        self.interval = self.interval.strip()

        if self.interval not in SUPPORTED_INTERVALS:
            raise ValueError(
                f"Unsupported INTERVAL={self.interval}. Supported values: "
                + ", ".join(sorted(SUPPORTED_INTERVALS))
            )
        if self.base_asset == self.quote_asset:
            raise ValueError("BASE_ASSET and QUOTE_ASSET must be different")
        if self.mode in {"testnet", "live"} and not (
            self.binance_api_key and self.binance_api_secret
        ):
            raise ValueError(
                "BINANCE_API_KEY and BINANCE_API_SECRET are required for testnet/live mode"
            )
        return self

    def ensure_directories(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.learning_profile_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
