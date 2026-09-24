import numpy as np
import pandas as pd

from app.models import SignalSide, StrategySignal
from app.strategy.indicators import enrich


class EnsembleStrategy:
    """A transparent baseline ensemble: trend + momentum + RSI + volume/volatility context."""

    def __init__(self, min_volume_ratio: float = 0.75, max_extension_atr: float = 2.0):
        self.min_volume_ratio = min_volume_ratio
        self.max_extension_atr = max_extension_atr

    def evaluate(self, raw_df: pd.DataFrame, has_position: bool) -> StrategySignal:
        columns = ["open", "high", "low", "close", "volume"]
        if not set(columns).issubset(raw_df.columns):
            return StrategySignal(SignalSide.HOLD, 0.0, "Missing OHLCV candle data")
        if len(raw_df) < 60:
            return StrategySignal(SignalSide.HOLD, 0.0, "Not enough candles")
        try:
            values = raw_df[columns].astype(float)
        except (ValueError, TypeError):
            return StrategySignal(SignalSide.HOLD, 0.0, "Invalid candle data")
        if (
            not np.isfinite(values.to_numpy()).all()
            or (values[["open", "high", "low", "close"]] <= 0).any().any()
            or (values.volume < 0).any()
            or (values.high < values[["open", "close", "low"]].max(axis=1)).any()
            or (values.low > values[["open", "close", "high"]].min(axis=1)).any()
        ):
            return StrategySignal(SignalSide.HOLD, 0.0, "Invalid candle data")
        df = enrich(values)
        # Never drop a bad latest indicator row and trade an older candle instead.
        x = df.iloc[-1]
        prev = df.iloc[-2]
        if not np.isfinite(df.iloc[-2:].to_numpy()).all():
            return StrategySignal(SignalSide.HOLD, 0.0, "Indicators unavailable for latest candles")

        trend_strength = (x.ema_fast - x.ema_slow) / x.close
        momentum = float(x.momentum_5)
        volume_ratio = float(x.volume_ratio)
        atr_pct = float(x.atr_pct)
        rsi = float(x.rsi)

        bullish = x.ema_fast > x.ema_slow and x.close > x.ema_fast and momentum > 0
        bearish = x.ema_fast < x.ema_slow and x.close < x.ema_fast and momentum < 0
        fresh_bull_cross = prev.ema_fast <= prev.ema_slow and x.ema_fast > x.ema_slow
        fresh_bear_cross = prev.ema_fast >= prev.ema_slow and x.ema_fast < x.ema_slow

        features = {
            "price": float(x.close),
            "ema_fast": float(x.ema_fast),
            "ema_slow": float(x.ema_slow),
            "rsi": rsi,
            "momentum_5": momentum,
            "volume_ratio": volume_ratio,
            "atr_pct": atr_pct,
            "trend_strength": float(trend_strength),
        }

        if not has_position and bullish and 43 <= rsi <= 70:
            if volume_ratio < self.min_volume_ratio:
                return StrategySignal(SignalSide.HOLD, 0.0, "Entry blocked: weak relative volume", features)
            if x.atr <= 0 or (x.close - x.ema_fast) > self.max_extension_atr * x.atr:
                return StrategySignal(SignalSide.HOLD, 0.0, "Entry blocked: price extended above EMA trend", features)
            confidence = 0.55
            confidence += min(abs(trend_strength) * 35, 0.12)
            confidence += min(max(momentum, 0) * 8, 0.10)
            if volume_ratio > 1.05:
                confidence += 0.05
            if fresh_bull_cross:
                confidence += 0.08
            if atr_pct > 0.04:
                confidence -= 0.08
            return StrategySignal(
                SignalSide.BUY,
                max(0.0, min(confidence, 0.95)),
                "Bullish EMA trend with positive momentum and acceptable RSI",
                features,
            )

        if has_position and (bearish or fresh_bear_cross or rsi >= 76):
            confidence = 0.60
            if bearish:
                confidence += 0.10
            if fresh_bear_cross:
                confidence += 0.10
            if rsi >= 80:
                confidence += 0.05
            return StrategySignal(
                SignalSide.SELL,
                min(confidence, 0.95),
                "Exit signal from bearish trend/cross or overbought RSI",
                features,
            )

        return StrategySignal(SignalSide.HOLD, 0.50, "No sufficiently strong setup", features)
