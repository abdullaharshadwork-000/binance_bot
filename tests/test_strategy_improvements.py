"""Tests for volatility-adaptive (ATR) stops, the trend-flip exit, and signal logging."""

import numpy as np
import pandas as pd
import pytest

from app.config import Settings
from app.models import SignalSide, StrategySignal
from app.risk.manager import RiskManager
from app.storage.db import TradingDB
from app.strategy.ensemble import EnsembleStrategy


def atr_signal(atr_pct: float, confidence: float = 0.9) -> StrategySignal:
    return StrategySignal(
        SignalSide.BUY,
        confidence,
        "test",
        features={"atr_pct": atr_pct},
    )


def test_atr_stops_disabled_use_fixed_distances():
    manager = RiskManager(Settings(_env_file=None))
    assert manager.atr_scaled_stop_distances(atr_signal(0.01)) is None


def test_atr_stops_scale_with_volatility():
    settings = Settings(_env_file=None, use_atr_stops=True, atr_stop_multiplier=2.0)
    manager = RiskManager(settings)

    quiet = manager.atr_scaled_stop_distances(atr_signal(0.004))
    volatile = manager.atr_scaled_stop_distances(atr_signal(0.02))

    assert quiet is not None and volatile is not None
    assert quiet[0] < volatile[0]
    # Reward-to-risk ratio is preserved on both ends.
    ratio = settings.take_profit_pct / settings.stop_loss_pct
    assert quiet[1] == pytest.approx(quiet[0] * ratio)
    assert volatile[1] == pytest.approx(volatile[0] * ratio)


def test_atr_stops_are_clamped_to_configured_bounds():
    settings = Settings(_env_file=None, use_atr_stops=True)
    manager = RiskManager(settings)

    too_quiet = manager.atr_scaled_stop_distances(atr_signal(0.00001))
    too_volatile = manager.atr_scaled_stop_distances(atr_signal(0.5))

    assert too_quiet is not None
    assert too_quiet[0] == pytest.approx(settings.atr_stop_min_pct)
    assert too_volatile is not None
    # The wider of the absolute cap and double the configured stop binds.
    assert too_volatile[0] == pytest.approx(
        min(settings.atr_stop_max_pct, settings.stop_loss_pct * 2)
    )


def test_atr_stops_fall_back_to_fixed_when_feature_is_missing_or_invalid():
    settings = Settings(_env_file=None, use_atr_stops=True)
    manager = RiskManager(settings)
    for signal in [
        StrategySignal(SignalSide.BUY, 0.9, "no features"),
        StrategySignal(SignalSide.BUY, 0.9, "empty features", features={}),
        StrategySignal(SignalSide.BUY, 0.9, "bad atr", features={"atr_pct": float("nan")}),
        StrategySignal(SignalSide.BUY, 0.9, "negative atr", features={"atr_pct": -0.01}),
        StrategySignal(SignalSide.BUY, 0.9, "zero atr", features={"atr_pct": 0.0}),
        StrategySignal(SignalSide.BUY, 0.9, "string atr", features={"atr_pct": "abc"}),
    ]:
        assert manager.atr_scaled_stop_distances(signal) is None


def test_atr_stops_flow_into_risk_decision_prices():
    settings = Settings(_env_file=None, use_atr_stops=True, atr_stop_multiplier=2.0)
    manager = RiskManager(settings)
    decision = manager.evaluate_entry(
        signal=atr_signal(0.008),
        price=100.0,
        equity=1000.0,
        daily_realized_pnl=0.0,
        threshold=0.7,
        risk_multiplier=1.0,
    )
    assert decision.allowed
    assert decision.stop_price < 100.0 * (1 - settings.stop_loss_pct)
    assert decision.take_profit_price > 100.0 * (1 + settings.take_profit_pct)
    # Risk sizing uses the (wider) ATR stop distance but is still exposure-capped.
    stop_distance = 100.0 - decision.stop_price
    by_risk = (1000.0 * settings.risk_per_trade) / (stop_distance + 100.0 * 2 * 0.001 + 100.0 * 2 * 0.0002)
    by_exposure = (1000.0 * settings.max_position_fraction) / (100.0 * 1.0002 * 1.001)
    assert decision.quantity == pytest.approx(min(by_risk, by_exposure), rel=0.02)


def make_flip_candles(close):
    close = np.asarray(close, dtype=float)
    n = len(close)
    return pd.DataFrame({
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.full(n, 1000.0),
    })


def test_trend_flip_exit_when_price_closes_below_both_emas():
    # Sharp decline followed by a flat stabilization: price is below both EMAs
    # but 5-candle momentum has recovered to non-negative, so only the
    # trend-flip exit (not the confirmed-bearish exit) should fire.
    rising = np.linspace(100, 160, 110)
    dropping = np.linspace(160, 112, 25)
    stabilizing = np.linspace(112, 113.5, 15)
    candles = make_flip_candles(np.concatenate([rising, dropping, stabilizing]))
    signal = EnsembleStrategy().evaluate(candles, has_position=True)
    assert signal.side == SignalSide.SELL
    assert "Trend flip" in signal.reason


def test_no_trend_flip_exit_without_position():
    n = 150
    rising = np.linspace(100, 160, 110)
    falling = np.linspace(160, 118, n - 110)
    candles = make_flip_candles(np.concatenate([rising, falling]))
    signal = EnsembleStrategy().evaluate(candles, has_position=False)
    assert signal.side == SignalSide.HOLD


def test_uptrend_with_position_is_retained():
    candles = make_flip_candles(np.linspace(100, 145, 150))
    signal = EnsembleStrategy().evaluate(candles, has_position=True)
    assert signal.side == SignalSide.HOLD


def test_record_signal_round_trips_features():
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db = TradingDB(str(Path(tmp) / "test.db"))
        db.init()
        db.record_signal(
            mode="paper",
            symbol="BTCUSDT",
            interval="15m",
            side="BUY",
            confidence=0.81,
            reason="test entry",
            price=100.0,
            candle_close_time=1234,
            features={"atr_pct": 0.008, "adx": 20.0},
            entry_checks={"ADX confirms a trending market": True},
            score_components={"Base qualifying setup": 0.55},
        )
        db.record_signal(
            mode="paper",
            symbol="BTCUSDT",
            interval="15m",
            side="HOLD",
            confidence=0.0,
            reason="Entry requirements not met",
        )
        with db.connection() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM signal_log ORDER BY id")]
        assert len(rows) == 2
        assert rows[0]["side"] == "BUY"
        assert json.loads(rows[0]["features"])["atr_pct"] == 0.008
        assert json.loads(rows[0]["entry_checks"])["ADX confirms a trending market"] is True
        assert rows[1]["features"] is None
        stats = db.signal_stats(mode="paper", symbol="BTCUSDT")
        assert stats["total"] == 2
        assert stats["by_side"]["BUY"]["count"] == 1
        assert stats["by_side"]["HOLD"]["count"] == 1


def test_signal_log_survives_reopen_with_existing_database():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "test.db")
        db = TradingDB(path)
        db.init()
        db.close_connection if hasattr(db, "close_connection") else None
        db2 = TradingDB(path)
        db2.init()  # must not fail on existing databases
        db2.record_signal(
            mode="testnet",
            symbol="ETHUSDT",
            interval="5m",
            side="SELL",
            confidence=0.75,
        )
        assert db2.signal_stats(mode="testnet", symbol="ETHUSDT")["total"] == 1
