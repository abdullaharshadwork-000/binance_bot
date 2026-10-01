import asyncio

import httpx
import pandas as pd

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
    class FakeStrategy:
        def __init__(self, **kwargs):
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

    result = comparison.backtest_frame(
        frame,
        settings=_settings(),
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
