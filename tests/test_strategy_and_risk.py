import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import SignalSide, StrategySignal
from app.risk.manager import RiskManager
from app.strategy.ensemble import EnsembleStrategy
from app.strategy.indicators import rsi


def make_candles(n=150):
    close = np.linspace(100, 130, n)
    return pd.DataFrame({
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.linspace(1000, 1300, n),
    })


def test_strategy_returns_valid_signal():
    signal = EnsembleStrategy().evaluate(make_candles(), has_position=False)
    assert signal.side in {SignalSide.BUY, SignalSide.HOLD, SignalSide.SELL}
    assert 0 <= signal.confidence <= 1


def test_risk_position_is_capped():
    settings = Settings(_env_file=None)
    manager = RiskManager(settings)
    signal = StrategySignal(SignalSide.BUY, 0.9, "test")
    decision = manager.evaluate_entry(
        signal=signal,
        price=100,
        equity=1000,
        daily_realized_pnl=0,
        threshold=0.62,
        risk_multiplier=1.0,
    )
    assert decision.allowed
    assert decision.quantity * 100 <= 1000 * settings.max_position_fraction + 1e-9


def test_rsi_rising_series_reaches_100():
    values = pd.Series(np.arange(1, 80, dtype=float))
    assert rsi(values, 14).iloc[-1] == pytest.approx(100.0)


def test_rsi_falling_series_reaches_zero():
    values = pd.Series(np.arange(80, 1, -1, dtype=float))
    assert rsi(values, 14).iloc[-1] == pytest.approx(0.0)


def test_rsi_flat_series_is_neutral():
    values = pd.Series(np.full(80, 100.0))
    assert rsi(values, 14).iloc[-1] == pytest.approx(50.0)


def test_invalid_interval_is_rejected_at_configuration_time():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, interval="7m")


def test_trailing_distance_must_fit_inside_profit_target():
    with pytest.raises(ValidationError, match="TRAILING_STOP_DISTANCE_PCT"):
        Settings(
            _env_file=None,
            take_profit_pct=0.01,
            trailing_stop_distance_pct=0.01,
        )


def quality_candles():
    x = np.arange(150)
    close = 100 + x * .03 + np.sin(x * .5 + 1) * .4
    return pd.DataFrame(dict(open=close, high=close + .3, low=close - .3,
                             close=close, volume=np.full(150, 1000.0)))


def test_entry_volume_filter_blocks_weak_participation():
    candles = quality_candles()
    assert EnsembleStrategy().evaluate(candles, False).side == SignalSide.BUY
    candles.loc[149, "volume"] = 100
    result = EnsembleStrategy().evaluate(candles, False)
    assert result.side == SignalSide.HOLD
    assert "volume" in result.reason
    assert EnsembleStrategy(min_volume_ratio=0).evaluate(candles, False).side == SignalSide.BUY


def test_extension_filter_is_configurable():
    candles = quality_candles()
    result = EnsembleStrategy(max_extension_atr=.1).evaluate(candles, False)
    assert result.side == SignalSide.HOLD
    assert "extended" in result.reason


@pytest.mark.parametrize("column,value", [("close", np.nan), ("close", np.inf),
    ("volume", -1), ("low", 10000), ("close", 0)])
def test_invalid_latest_candle_never_falls_back_to_older_signal(column, value):
    candles = quality_candles()
    candles.loc[149, column] = value
    result = EnsembleStrategy().evaluate(candles, False)
    assert result.side == SignalSide.HOLD
    assert result.confidence == 0
    assert "Invalid" in result.reason


def test_unavailable_latest_volume_indicator_does_not_use_old_candle():
    candles = quality_candles()
    candles.loc[130:, "volume"] = 0
    result = EnsembleStrategy().evaluate(candles, False)
    assert result.side == SignalSide.HOLD
    assert "Indicators unavailable" in result.reason


def test_entry_filters_do_not_suppress_bearish_exit():
    candles = make_candles()
    for column in ["open", "high", "low", "close"]:
        candles[column] = candles[column].iloc[::-1].to_numpy()
    candles.loc[149, "volume"] = 1
    assert EnsembleStrategy().evaluate(candles, True).side == SignalSide.SELL


def test_weak_crossover_is_blocked_without_suppressing_established_trend():
    candles = quality_candles()
    shift = np.arange(len(candles)) * .025
    for column in ["open", "high", "low", "close"]:
        candles[column] -= shift
    assert EnsembleStrategy(min_trend_atr=0).evaluate(candles, False).side == SignalSide.BUY
    signal = EnsembleStrategy().evaluate(candles, False)
    assert signal.side == SignalSide.HOLD
    assert "trend separation" in signal.reason
    assert 0 < signal.features["trend_separation_atr"] < .25
    assert EnsembleStrategy().evaluate(quality_candles(), False).side == SignalSide.BUY
