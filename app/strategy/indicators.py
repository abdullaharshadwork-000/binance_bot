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


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = ema(out["close"], 20)
    out["ema_slow"] = ema(out["close"], 50)
    out["rsi"] = rsi(out["close"], 14)
    out["atr"] = atr(out, 14)
    out["momentum_5"] = out["close"].pct_change(5, fill_method=None)
    out["volume_mean_20"] = out["volume"].rolling(20).mean()
    out["volume_ratio"] = out["volume"] / out["volume_mean_20"].replace(0, np.nan)
    out["atr_pct"] = out["atr"] / out["close"]
    return out.reset_index(drop=True)
