import pandas as pd

from app.models import SignalSide, StrategySignal
from app.strategy.indicators import enrich


class EnsembleStrategy:
    """A transparent baseline ensemble: trend + momentum + RSI + volume/volatility context."""

    def evaluate(self, raw_df: pd.DataFrame, has_position: bool) -> StrategySignal:
        df = enrich(raw_df)
        if len(df) < 60:
            return StrategySignal(SignalSide.HOLD, 0.0, "Not enough candles")

        x = df.iloc[-1]
        prev = df.iloc[-2]

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
