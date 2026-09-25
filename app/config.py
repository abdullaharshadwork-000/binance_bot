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

    # Optional comma-separated symbols sharing QUOTE_ASSET.
    # Example: BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT
    symbols: str = ""
    max_concurrent_positions: int = Field(default=2, ge=1, le=10)
    allow_multi_symbol_live: bool = False

    interval: str = "15m"

    # Fast market/risk loop. Strategy still uses completed candles only.
    cycle_seconds: float = Field(default=1.0, ge=0.5, le=60)
    use_websocket_market_data: bool = True
    market_data_stale_seconds: float = Field(default=3.0, ge=1.0, le=30.0)
    strategy_refresh_grace_seconds: float = Field(default=1.0, ge=0.0, le=10.0)
    account_refresh_seconds: float = Field(default=5.0, ge=1.0, le=60.0)
    account_max_age_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    max_entry_deviation_pct: float = Field(default=0.015, gt=0, le=0.10)
    max_portfolio_exposure_fraction: float = Field(default=0.15, gt=0, le=1.0)
    entry_cooldown_seconds: float = Field(default=60.0, ge=0, le=86400)

    binance_api_key: str = ""
    binance_api_secret: str = ""

    paper_starting_balance: float = Field(default=1000.0, gt=0)
    risk_per_trade: float = Field(default=0.0025, gt=0, le=0.02)
    max_position_fraction: float = Field(default=0.05, gt=0, le=0.50)
    max_daily_loss_fraction: float = Field(default=0.01, gt=0, le=0.10)
    stop_loss_pct: float = Field(default=0.012, gt=0, le=0.10)
    take_profit_pct: float = Field(default=0.024, gt=0, le=0.30)
    enable_trailing_stop: bool = True
    trailing_stop_activation_pct: float = Field(default=0.01, gt=0, le=0.20)
    trailing_stop_distance_pct: float = Field(default=0.008, gt=0, le=0.10)
    breakeven_activation_pct: float = Field(default=0.006, gt=0, le=0.20)
    strategy_min_trend_atr: float = Field(default=0.25, ge=0, le=5)
    max_consecutive_losses: int = Field(default=3, ge=1, le=20)
    loss_streak_pause_seconds: float = Field(default=3600, ge=0, le=86400)
    strategy_min_volume_ratio: float = Field(default=0.75, ge=0, le=5)
    strategy_max_extension_atr: float = Field(default=2.0, gt=0, le=10)
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
        parsed_symbols = self.trading_symbols
        if not parsed_symbols:
            raise ValueError("At least one trading symbol is required")
        if self.account_max_age_seconds < self.account_refresh_seconds:
            raise ValueError("ACCOUNT_MAX_AGE_SECONDS must cover ACCOUNT_REFRESH_SECONDS")

        if parsed_symbols:
            self.symbol = parsed_symbols[0]
            derived_base = self.base_for_symbol(self.symbol)
            if derived_base is None:
                raise ValueError(
                    f"Primary symbol {self.symbol} must end with QUOTE_ASSET={self.quote_asset}"
                )
            self.base_asset = derived_base

        if self.interval not in SUPPORTED_INTERVALS:
            raise ValueError(
                f"Unsupported INTERVAL={self.interval}. Supported values: "
                + ", ".join(sorted(SUPPORTED_INTERVALS))
            )
        if self.base_asset == self.quote_asset:
            raise ValueError("BASE_ASSET and QUOTE_ASSET must be different")
        if self.trailing_stop_distance_pct >= self.take_profit_pct:
            raise ValueError(
                "TRAILING_STOP_DISTANCE_PCT must be smaller than TAKE_PROFIT_PCT"
            )

        for symbol in parsed_symbols:
            if self.base_for_symbol(symbol) is None:
                raise ValueError(
                    f"Multi-symbol entry {symbol} must end with QUOTE_ASSET={self.quote_asset}"
                )

        if len(parsed_symbols) > 1 and self.mode == "live" and not self.allow_multi_symbol_live:
            raise ValueError(
                "Multi-symbol live trading is blocked. Keep ALLOW_MULTI_SYMBOL_LIVE=false "
                "until the multi-symbol engine has been validated in Testnet."
            )

        if self.mode in {"testnet", "live"} and not (
            self.binance_api_key and self.binance_api_secret
        ):
            raise ValueError(
                "BINANCE_API_KEY and BINANCE_API_SECRET are required for testnet/live mode"
            )
        return self

    @property
    def trading_symbols(self) -> list[str]:
        raw = self.symbols.strip()
        values = [self.symbol] if not raw else raw.split(",")
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            symbol = value.strip().upper()
            if not symbol or symbol in seen:
                continue
            result.append(symbol)
            seen.add(symbol)
        return result

    def base_for_symbol(self, symbol: str) -> str | None:
        symbol = symbol.strip().upper()
        quote = self.quote_asset.strip().upper()
        if not quote or not symbol.endswith(quote):
            return None
        base = symbol[: -len(quote)]
        return base or None

    def ensure_directories(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.learning_profile_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
