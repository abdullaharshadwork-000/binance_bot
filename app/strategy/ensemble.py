import numpy as np
import pandas as pd

from app.models import SignalSide, StrategySignal
from app.strategy.indicators import enrich


class EnsembleStrategy:
    """A transparent baseline ensemble: trend + momentum + RSI + volume/volatility context."""

    def __init__(self, min_volume_ratio: float = 0.75, max_extension_atr: float = 2.0, min_trend_atr: float = 0.25):
        self.min_trend_atr = min_trend_atr
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

        features = {
            "price": float(x.close),
            "ema_fast": float(x.ema_fast),
            "ema_slow": float(x.ema_slow),
            "rsi": rsi,
            "momentum_5": momentum,
            "volume_ratio": volume_ratio,
            "atr_pct": atr_pct,
            "trend_strength": float(trend_strength),
            "trend_separation_atr": float((x.ema_fast - x.ema_slow) / x.atr) if x.atr > 0 else 0.0,
        }

        checks = {
            "Fast EMA above slow EMA": bool(x.ema_fast > x.ema_slow),
            "Close above fast EMA": bool(x.close > x.ema_fast),
            "Positive 5-candle momentum": momentum > 0,
            "RSI between 43 and 70": 43 <= rsi <= 70,
            "Sufficient EMA separation": features["trend_separation_atr"] >= self.min_trend_atr,
            "Sufficient relative volume": volume_ratio >= self.min_volume_ratio,
            "Price not overextended": bool(x.atr > 0 and (x.close - x.ema_fast) <= self.max_extension_atr * x.atr),
        }

        if not has_position and bullish and 43 <= rsi <= 70:
            if features["trend_separation_atr"] < self.min_trend_atr:
                return StrategySignal(SignalSide.HOLD, 0.0, "Entry blocked: weak EMA trend separation", features, entry_checks=checks)
            if volume_ratio < self.min_volume_ratio:
                return StrategySignal(SignalSide.HOLD, 0.0, "Entry blocked: weak relative volume", features, entry_checks=checks)
            if x.atr <= 0 or (x.close - x.ema_fast) > self.max_extension_atr * x.atr:
                return StrategySignal(SignalSide.HOLD, 0.0, "Entry blocked: price extended above EMA trend", features, entry_checks=checks)
            components = {
                "Base qualifying setup": 0.55,
                "EMA strength": min(abs(trend_strength) * 35, 0.12),
                "Momentum": min(max(momentum, 0) * 8, 0.10),
                "Volume bonus": 0.05 if volume_ratio > 1.05 else 0.0,
                "Fresh crossover bonus": 0.08 if fresh_bull_cross else 0.0,
                "High volatility penalty": -0.08 if atr_pct > 0.04 else 0.0,
            }
            confidence = sum(components.values())
            return StrategySignal(
                SignalSide.BUY,
                max(0.0, min(confidence, 0.95)),
                "Bullish EMA trend with positive momentum and acceptable RSI",
                features,
                entry_checks=checks,
                score_components=components,
            )

        if has_position and bearish:
            confidence = 0.75
            if features["trend_separation_atr"] >= self.min_trend_atr * 1.5:
                confidence += 0.10
            return StrategySignal(
                SignalSide.SELL,
                min(confidence, 0.95),
                "Exit signal from confirmed bearish trend",
                features,
                entry_checks=checks,
            )

        failed = [name for name, passed in checks.items() if not passed]
        reason = "Position retained; no strategy exit" if has_position else "Entry requirements not met: " + "; ".join(failed)
        return StrategySignal(SignalSide.HOLD, 0.0, reason, features, entry_checks=checks)
