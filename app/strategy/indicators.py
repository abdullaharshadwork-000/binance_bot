import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()

    rs = gain / loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))

    only_gains = (gain > 0) & (loss == 0)
    only_losses = (loss > 0) & (gain == 0)
    flat = (gain == 0) & (loss == 0)

    out = out.where(~only_gains, 100.0)
    out = out.where(~only_losses, 0.0)
    out = out.where(~flat, 50.0)
    return out.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False).mean()


def directional_index(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return Wilder-smoothed ADX, +DI and -DI for trend-strength confirmation."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    smoothed_tr = atr(df, period).replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / smoothed_tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / smoothed_tr
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / denominator).replace(
        [np.inf, -np.inf], np.nan
    )
    adx_value = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx_value.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = ema(out["close"], 20)
    out["ema_slow"] = ema(out["close"], 50)
    out["rsi"] = rsi(out["close"], 14)
    out["atr"] = atr(out, 14)
    out["adx"], out["plus_di"], out["minus_di"] = directional_index(out, 14)
    out["ema_slow_slope_5"] = out["ema_slow"].pct_change(5, fill_method=None)
    out["momentum_5"] = out["close"].pct_change(5, fill_method=None)
    out["volume_mean_20"] = out["volume"].rolling(20).mean()
    out["volume_ratio"] = out["volume"] / out["volume_mean_20"].replace(0, np.nan)
    out["atr_pct"] = out["atr"] / out["close"]
    return out.reset_index(drop=True)
