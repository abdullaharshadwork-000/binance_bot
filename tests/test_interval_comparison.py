import asyncio

import httpx
import pandas as pd
import pytest

import compare_strategy_intervals as comparison
from app.config import Settings
from app.models import SignalSide, StrategySignal


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        mode="paper",
        symbol="BTCUSDT",
        symbols="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        interval="5m",
        min_signal_confidence=0.62,
        trading_fee_bps=10,
        paper_slippage_bps=2,
        stop_loss_pct=0.012,
        take_profit_pct=0.024,
        enable_trailing_stop=True,
        breakeven_activation_pct=0.006,
        trailing_stop_activation_pct=0.01,
        trailing_stop_distance_pct=0.008,
    )


def test_trade_costs_are_progressively_more_conservative():
    result = comparison.trade_costs(
        entry_market=100.0,
        exit_market=102.0,
        notional=3000.0,
        fee_bps=10.0,
        slippage_bps=2.0,
        extra_stress_slippage_bps=2.0,
    )

    assert result["raw_pnl"] > result["fee_adjusted_pnl"]
    assert result["fee_adjusted_pnl"] > result["cost_adjusted_pnl"]
    assert result["cost_adjusted_pnl"] > result["stress_adjusted_pnl"]


def test_dynamic_universe_comparison_uses_persisted_symbols_and_pins(tmp_path):
    settings = Settings(
        _env_file=None,
        mode="paper",
        symbol="BTCUSDT",
        quote_asset="USDT",
        universe_mode="top_volume",
        universe_pinned_symbols="BNBUSDT",
        database_path=str(tmp_path / "trading.db"),
    )
    db = comparison.TradingDB(settings.database_path)
    db.init()
    db.save_trading_universe(mode="paper", symbols=["ETHUSDT", "SOLUSDT"])
    db.open_trade(
        mode="paper",
        symbol="XRPUSDT",
        quantity=1.0,
        entry_price=1.0,
        entry_fee=0.0,
        reason="test open position",
        stop_price=0.9,
        take_profit_price=1.1,
    )

    # Config fallback is not a substitute for the selected dynamic universe.
    assert settings.trading_symbols == ["BTCUSDT"]
    assert comparison.symbols_for_comparison(settings) == [
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "XRPUSDT",
    ]


def test_dynamic_universe_comparison_fails_without_a_selected_universe(tmp_path):
    database_path = tmp_path / "trading.db"
    settings = Settings(
        _env_file=None,
        mode="paper",
        symbol="BTCUSDT",
        quote_asset="USDT",
        universe_mode="top_volume",
        database_path=str(database_path),
    )

    with pytest.raises(RuntimeError, match="No dynamic universe is recorded"):
        comparison.symbols_for_comparison(settings)
    assert not database_path.exists()


def test_short_history_returns_zero_trades():
    frame = pd.DataFrame(
        [
            {
                "open_time": i * 300_000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1000.0,
                "close_time": i * 300_000 + 299_999,
            }
            for i in range(20)
        ]
    )

    result = comparison.backtest_frame(
        frame,
        settings=_settings(),
        interval="5m",
        symbol="BTCUSDT",
        notional=3000.0,
        extra_stress_slippage_bps=2.0,
    )

    assert result["raw"]["trades"] == 0
    assert result["stress_adjusted"]["total_pnl"] == 0.0


def test_backtest_enters_on_next_open_and_can_hit_take_profit(monkeypatch):
    strategy_arguments = {}

    class FakeStrategy:
        def __init__(self, **kwargs):
            strategy_arguments.update(kwargs)
            self.calls = 0

        def evaluate(self, raw_df, has_position):
            self.calls += 1
            if not has_position and self.calls == 1:
                return StrategySignal(
                    SignalSide.BUY,
                    0.80,
                    "test buy",
                )
            return StrategySignal(SignalSide.HOLD, 0.0, "hold")

    monkeypatch.setattr(comparison, "EnsembleStrategy", FakeStrategy)

    rows = []
    for i in range(65):
        open_price = 100.0
        high = 100.5
        if i == 61:
            high = 103.0
        rows.append(
            {
                "open_time": i * 300_000,
                "open": open_price,
                "high": high,
                "low": 99.8,
                "close": 100.2,
                "volume": 1000.0,
                "close_time": i * 300_000 + 299_999,
            }
        )
    frame = pd.DataFrame(rows)
    settings = _settings()
    settings.strategy_min_adx = 27.0

    result = comparison.backtest_frame(
        frame,
        settings=settings,
        interval="5m",
        symbol="BTCUSDT",
        notional=3000.0,
        extra_stress_slippage_bps=2.0,
    )

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_time"] == rows[61]["open_time"]
    assert trade["reason"] == "Take profit"
    assert trade["raw_pnl"] > 0
    assert strategy_arguments["min_adx"] == 27.0


def test_combined_results_keep_per_symbol_and_chronological_metrics():
    base = {
        "interval": "5m",
        "candles": 100,
        "fee_adjusted": {},
        "cost_adjusted": {},
        "stress_adjusted": {},
    }
    btc = {
        **base,
        "symbol": "BTCUSDT",
        "trades": [
            {
                "entry_time": 1,
                "exit_time": 3,
                "entry_price": 100,
                "exit_price": 101,
                "reason": "x",
                "raw_pnl": 10,
                "fee_adjusted_pnl": 8,
                "cost_adjusted_pnl": 7,
                "stress_adjusted_pnl": 6,
            }
        ],
        "raw": comparison.summarize([10]),
    }
    eth = {
        **base,
        "symbol": "ETHUSDT",
        "trades": [
            {
                "entry_time": 1,
                "exit_time": 2,
                "entry_price": 100,
                "exit_price": 99,
                "reason": "x",
                "raw_pnl": -5,
                "fee_adjusted_pnl": -7,
                "cost_adjusted_pnl": -8,
                "stress_adjusted_pnl": -9,
            }
        ],
        "raw": comparison.summarize([-5]),
    }

    result = comparison.combine_symbol_results([btc, eth], "5m")

    assert result["trades"] == 2
    assert result["raw"]["total_pnl"] == 5
    assert result["stress_adjusted"]["total_pnl"] == -3
    assert result["per_symbol"]["BTCUSDT"]["trades"] == 1
    assert result["per_symbol"]["ETHUSDT"]["trades"] == 1


def test_kline_fetch_retries_timeout_then_succeeds(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [[1, "1", "2", "0.5", "1.5", "10", 2]]

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def get(self, path, params):
            self.calls += 1
            if self.calls == 1:
                request = httpx.Request("GET", "https://api.binance.com/api/v3/klines")
                raise httpx.ReadTimeout("slow", request=request)
            return FakeResponse()

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(comparison.asyncio, "sleep", no_sleep)
    client = FakeClient()
    result = asyncio.run(
        comparison._get_klines_with_retry(
            client,
            params={"symbol": "BTCUSDT"},
            symbol="BTCUSDT",
            interval="1m",
            retries=2,
        )
    )

    assert client.calls == 2
    assert result[0][0] == 1


def test_history_checkpoint_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(comparison, "CACHE_DIR", tmp_path)
    now_ms = 1_000_000
    rows = [
        {
            "open_time": 1,
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 10.0,
            "close_time": 2,
        }
    ]

    comparison._save_history_checkpoint(
        symbol="BTCUSDT",
        interval="1m",
        days=7,
        end_ms=now_ms,
        cursor=123,
        rows=rows,
        complete=False,
    )
    loaded_rows, cursor, end_ms, complete = comparison._load_history_checkpoint(
        symbol="BTCUSDT",
        interval="1m",
        days=7,
        now_ms=now_ms,
    )

    assert loaded_rows == rows
    assert cursor == 123
    assert end_ms == now_ms
    assert complete is False


def _wf_candles(n=600, seed=7):
    import numpy as np

    rng = np.random.default_rng(seed)
    steps = rng.normal(loc=0.0005, scale=0.01, size=n)
    close = 100 * np.exp(np.cumsum(steps))
    interval_ms = 15 * 60 * 1000
    open_times = np.arange(n, dtype=np.int64) * interval_ms
    return pd.DataFrame({
        "open_time": open_times,
        "open": close * (1 - 0.0005),
        "high": close * 1.004,
        "low": close * 0.996,
        "close": close,
        "volume": rng.uniform(900, 1100, n),
        "close_time": open_times + interval_ms - 1,
    })


def test_wf_slices_cover_frame_and_respect_warmup():
    slices = comparison._wf_slices(603, folds=3)
    assert slices[0][0] == 60
    assert slices[-1][1] == 603
    for (_, start_end_prev), (start, _) in zip(slices, slices[1:], strict=False):
        assert start == start_end_prev


def test_walk_forward_symbol_reports_folds_and_totals():
    settings = _settings()
    frame = _wf_candles()
    result = comparison.walk_forward_symbol(
        frame,
        settings=settings,
        interval="15m",
        symbol="BTCUSDT",
        notional=3000.0,
        extra_stress_slippage_bps=2.0,
        folds=3,
    )
    assert result["symbol"] == "BTCUSDT"
    assert len(result["fold_reports"]) == 2
    for report in result["fold_reports"]:
        assert "skipped" not in report
        assert report["train_candles"] >= comparison.WF_MIN_SEGMENT_CANDLES
        assert report["test_candles"] >= comparison.WF_MIN_SEGMENT_CANDLES
        assert set(report["best_params"]) == {
            "strategy_min_adx",
            "strategy_min_volume_ratio",
            "use_atr_stops",
        }
    # Baseline out-of-sample PnL is always computed with the current config.
    assert isinstance(result["baseline_oos_stress_pnl"], float)
    assert (
        result["optimized_oos_stress_pnl"]
        == pytest.approx(sum(r["out_of_sample_stress_pnl"] for r in result["fold_reports"]))
    )


def test_walk_forward_symbol_skips_undersized_segments():
    settings = _settings()
    frame = _wf_candles(n=130)
    result = comparison.walk_forward_symbol(
        frame,
        settings=settings,
        interval="15m",
        symbol="BTCUSDT",
        notional=3000.0,
        extra_stress_slippage_bps=2.0,
        folds=3,
    )
    assert all("skipped" in report for report in result["fold_reports"])
    assert result["optimized_oos_trades"] == 0


class _ControlledStrategy:
    """Fake strategy with a configurable signal-candle ATR feature.

    Always BUYs when flat and holds while in position so that simulated exits
    come from the protective stops, not from strategy signals.
    """

    def __init__(self, atr_pct):
        self.atr_pct = atr_pct

    def evaluate(self, history, has_position):
        if has_position:
            return StrategySignal(SignalSide.HOLD, 0.0, "fake hold")
        return StrategySignal(
            SignalSide.BUY,
            0.9,
            "fake entry",
            features={"atr_pct": self.atr_pct},
        )


def _entry_control_candles(n=70):
    import numpy as np

    close = np.full(n, 100.0)
    close[62] = 99.0   # 1% dip after entry: hits a 0.8% ATR stop, misses the 1.2% fixed stop
    close[63:] = 103.0
    interval_ms = 15 * 60 * 1000
    open_times = np.arange(n, dtype=np.int64) * interval_ms
    return pd.DataFrame({
        "open_time": open_times,
        "open": close,
        "high": close * 1.001,
        "low": close * 0.999,
        "close": close,
        "volume": np.full(n, 1000.0),
        "close_time": open_times + interval_ms - 1,
    })


def test_backtest_simulates_atr_scaled_stops(monkeypatch):
    settings = _settings().model_copy(update={
        "enable_trailing_stop": False,
        "entry_cooldown_seconds": 0,
        "use_atr_stops": True,
        "atr_stop_multiplier": 2.0,
    })
    # Low-volatility signal candle -> 2 x 0.4% = 0.8% stop (vs 1.2% fixed).
    monkeypatch.setattr(comparison, "EnsembleStrategy", lambda **_: _ControlledStrategy(0.004))

    atr_result = comparison.backtest_frame(
        _entry_control_candles(), settings=settings, interval="15m",
        symbol="BTCUSDT", notional=3000.0, extra_stress_slippage_bps=0.0,
    )
    fixed_result = comparison.backtest_frame(
        _entry_control_candles(),
        settings=settings.model_copy(update={"use_atr_stops": False}),
        interval="15m", symbol="BTCUSDT", notional=3000.0,
        extra_stress_slippage_bps=0.0,
    )

    atr_trades = atr_result["trades"]
    assert atr_trades, "expected at least one simulated trade"
    assert atr_trades[0]["stop_pct"] == pytest.approx(0.008)
    assert atr_trades[0]["reason"] == "Stop loss"

    assert fixed_result["trades"], "expected at least one simulated trade"
    assert fixed_result["trades"][0]["stop_pct"] == pytest.approx(settings.stop_loss_pct)
    assert fixed_result["trades"][0]["reason"] != "Stop loss"


def test_backtest_atr_stops_missing_feature_falls_back_to_fixed(monkeypatch):
    settings = _settings().model_copy(update={
        "enable_trailing_stop": False,
        "use_atr_stops": True,
    })
    monkeypatch.setattr(comparison, "EnsembleStrategy", lambda **_: _ControlledStrategy(None))
    result = comparison.backtest_frame(
        _entry_control_candles(), settings=settings, interval="15m",
        symbol="BTCUSDT", notional=3000.0, extra_stress_slippage_bps=0.0,
    )
    assert result["trades"][0]["stop_pct"] == pytest.approx(settings.stop_loss_pct)
