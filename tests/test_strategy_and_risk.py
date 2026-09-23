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
