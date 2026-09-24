import asyncio
import time
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.agents.orchestrator import TradingOrchestrator
from app.config import Settings
from app.models import ExecutionResult, SignalSide, StrategySignal


def make_bot(tmp_path):
    settings = Settings(
        _env_file=None,
        mode="paper",
        enable_llm_advisor=False,
        enable_adaptive_learning=False,
        database_path=str(tmp_path / "trades.db"),
        learning_profile_path=str(tmp_path / "learning.json"),
    )
    bot = TradingOrchestrator(settings)
    bot._exchange_validated = True
    bot._market_price = AsyncMock(return_value=(100.0, "test", 0.0))
    return bot


def open_position(bot):
    bot.db.open_trade(
        mode="paper",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="test",
        stop_price=95,
        take_profit_price=110,
    )
    bot.db.set_state(bot.broker._paper_base_key, 1)


def test_out_of_order_websocket_tick_cannot_replace_newer_price(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        try:
            await bot._on_price_tick(101.0, 2_000)
            await bot._on_price_tick(99.0, 1_000)

            assert bot.latest_price == 101.0
            assert bot.latest_price_event_ms == 2_000
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_price", [94.0, 111.0])
def test_slow_strategy_refresh_does_not_block_later_protective_exit(tmp_path, exit_price):
    async def scenario():
        bot = make_bot(tmp_path)
        open_position(bot)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_compute(force=False):
            started.set()
            await release.wait()
            return None

        bot._compute_strategy = slow_compute
        try:
            await asyncio.wait_for(bot._cycle(), 1)
            await asyncio.wait_for(started.wait(), 1)

            bot._market_price.return_value = (exit_price, "test", 0.0)
            result = await asyncio.wait_for(bot._cycle(), 1)

            assert result["execution"]["action"] == "SELL"
            assert bot.db.get_open_trade("BTCUSDT", "paper") is None
            assert bot.strategy_task is not None
            assert not bot.strategy_task.done()
        finally:
            release.set()
            bot.running = True
            await bot.stop()

    asyncio.run(scenario())


def test_failed_manual_refresh_cannot_enter_from_cached_buy(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        bot.cached_signal = StrategySignal(SignalSide.BUY, 0.95, "Old signal")
        bot._compute_strategy = AsyncMock(side_effect=RuntimeError("Offline"))
        bot.broker.enter = AsyncMock()
        try:
            result = await bot.run_once()
            assert result["risk"]["allowed"] is False
            assert result["strategy_error"] == "Offline"
            bot.broker.enter.assert_not_awaited()
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())


def test_background_strategy_candidate_is_not_consumed_until_downstream_ready(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)

        candidate = {
            "signal": StrategySignal(SignalSide.BUY, 0.95, "New signal"),
            "candle_close_time": 123,
            "signal_price": 101.0,
            "llm_adjustment": 0.0,
            "llm_reason": "Not requested",
        }

        async def compute(force=False):
            return candidate

        bot._compute_strategy = compute
        bot.broker.enter = AsyncMock(
            return_value=ExecutionResult("BUY", True, "Entered")
        )
        try:
            first = await bot._cycle()
            assert first["risk"]["allowed"] is False

            # Let background strategy finish, then the next cycle consumes it.
            if bot.strategy_task is not None:
                await bot.strategy_task

            bot._market_price.return_value = (102.0, "test", 0.0)
            second = await bot._cycle()

            assert second["risk"]["allowed"] is True
            assert bot.broker.enter.await_args.args[2] == 102.0
            assert bot.db.get_state(bot._entry_state_key) == "123"
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())


def test_old_exchange_tick_is_not_considered_fresh(tmp_path):
    bot = make_bot(tmp_path)
    bot.latest_price = 100.0
    bot.latest_price_source = "websocket"
    bot.latest_price_received_monotonic = time.monotonic()
    bot.latest_price_event_ms = int(time.time() * 1000) - 3_600_000

    snapshot = bot.market_snapshot()

    assert snapshot["websocket_connected"] is False
    assert snapshot["exchange_age_ms"] >= 3_500_000


def test_incomplete_candles_never_reach_strategy(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        now_ms = int(time.time() * 1000)
        rows = []
        for i in range(70):
            close_time = now_ms + 60_000 if i >= 20 else now_ms - (70 - i) * 60_000
            rows.append(
                {
                    "open_time": close_time - 60_000,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 10.0,
                    "close_time": close_time,
                }
            )

        bot.exchange.klines = AsyncMock(return_value=pd.DataFrame(rows))

        with pytest.raises(RuntimeError, match="Not enough completed candles"):
            await bot._compute_strategy(force=True)

        await bot.exchange.close()

    asyncio.run(scenario())


def test_manual_cycle_is_rejected_while_bot_is_running(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        bot.running = True
        with pytest.raises(RuntimeError, match="disabled while"):
            await bot.run_once()
        bot.running = False
        await bot.exchange.close()

    asyncio.run(scenario())


def test_read_only_analysis_never_submits_or_closes_orders(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        candidate = {
            "signal": StrategySignal(SignalSide.BUY, 0.95, "Strong read-only signal"),
            "candle_close_time": 123,
            "signal_price": 101.0,
            "llm_adjustment": 0.0,
            "llm_reason": "Not requested",
        }
        bot._compute_strategy = AsyncMock(return_value=candidate)
        bot.broker.equity = AsyncMock(return_value=1000.0)
        bot.broker.enter = AsyncMock()
        bot.broker.maybe_exit = AsyncMock()

        try:
            result = await bot.analyze_once()

            assert result["read_only"] is True
            assert result["signal"]["side"] == "BUY"
            assert result["risk"]["allowed"] is True
            assert result["execution"]["action"] == "NONE"
            assert "no order" in result["execution"]["message"].lower()
            bot.broker.enter.assert_not_awaited()
            bot.broker.maybe_exit.assert_not_awaited()
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())


def test_profitable_position_ratchets_stop_and_persists_high_water_mark(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        open_position(bot)
        bot.exchange.normalize_price = AsyncMock(side_effect=lambda symbol, value: value)

        result = await bot.broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0.5, "hold"),
            106.0,
        )

        trade = bot.db.get_open_trade("BTCUSDT", "paper")
        assert result.action == "HOLD"
        assert "stop raised" in result.message.lower()
        assert trade["highest_price"] == pytest.approx(106.0)
        assert trade["stop_price"] == pytest.approx(106 * (1 - 0.008))

        # A pullback cannot loosen the persisted stop.
        second = await bot.broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0.5, "hold"),
            105.5,
        )
        trade = bot.db.get_open_trade("BTCUSDT", "paper")
        assert second.action == "HOLD"
        assert trade["highest_price"] == pytest.approx(106.0)
        assert trade["stop_price"] == pytest.approx(106 * (1 - 0.008))
        await bot.exchange.close()

    asyncio.run(scenario())
