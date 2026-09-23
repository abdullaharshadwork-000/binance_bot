import asyncio
from unittest.mock import AsyncMock

import pytest

from app.agents.orchestrator import TradingOrchestrator
from app.config import Settings
from app.models import ExecutionResult, SignalSide, StrategySignal


def make_bot(tmp_path):
    settings = Settings(
        _env_file=None, mode="paper", enable_llm_advisor=False,
        database_path=str(tmp_path / "trades.db"),
        learning_profile_path=str(tmp_path / "learning.json"),
    )
    bot = TradingOrchestrator(settings)
    bot._market_price = AsyncMock(return_value=(100.0, "test", 0.0))
    return bot


def open_position(bot):
    bot.db.open_trade(
        mode="paper", symbol="BTCUSDT", quantity=1, entry_price=100,
        entry_fee=0, reason="test", stop_price=95, take_profit_price=110,
    )
    bot.db.set_state("paper_base_balance", 1)


@pytest.mark.parametrize("exit_price", [94.0, 111.0])
def test_slow_refresh_does_not_block_later_protective_exit(tmp_path, exit_price):
    async def scenario():
        bot = make_bot(tmp_path)
        open_position(bot)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_refresh(force=False):
            started.set()
            await release.wait()
            return False

        bot._refresh_strategy = slow_refresh
        try:
            await asyncio.wait_for(bot._cycle(), 1)
            await asyncio.wait_for(started.wait(), 1)
            bot._market_price.return_value = (exit_price, "test", 0.0)
            result = await asyncio.wait_for(bot._cycle(), 1)
            assert result["execution"]["action"] == "SELL"
            assert bot.db.get_open_trade("BTCUSDT") is None
            assert not bot.strategy_task.done()
        finally:
            bot.running = True
            task = bot.strategy_task
            await bot.stop()
            assert task.cancelled()
            assert bot.strategy_task is None

    asyncio.run(scenario())


def test_failed_refresh_preserves_exit_and_reports_error(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        open_position(bot)
        bot._market_price.return_value = (94.0, "test", 0.0)

        async def failed_refresh(force=False):
            assert bot.db.get_open_trade("BTCUSDT") is None
            raise RuntimeError("Candle feed unavailable")

        bot._refresh_strategy = failed_refresh
        try:
            result = await bot.run_once()
            assert result["execution"]["action"] == "SELL"
            assert result["strategy_error"] == "Candle feed unavailable"
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())


def test_failed_manual_refresh_cannot_enter_from_cached_buy(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        bot.cached_signal = StrategySignal(SignalSide.BUY, 0.95, "Old signal")
        bot._refresh_strategy = AsyncMock(side_effect=RuntimeError("Offline"))
        bot.broker.enter = AsyncMock()
        try:
            result = await bot.run_once()
            assert result["risk"]["allowed"] is False
            assert result["strategy_error"] == "Offline"
            bot.broker.enter.assert_not_awaited()
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())


def test_background_refresh_result_allows_entry_at_current_price(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)

        async def refresh(force=False):
            bot.cached_signal = StrategySignal(SignalSide.BUY, 0.95, "New signal")
            bot.cached_signal_candle_close_time = 123
            return True

        bot._refresh_strategy = refresh
        bot.broker.enter = AsyncMock(return_value=ExecutionResult("BUY", True, "Entered"))
        try:
            first = await bot._cycle()
            assert not first["risk"]["allowed"]
            await bot.strategy_task
            bot._market_price.return_value = (102.0, "test", 0.0)
            second = await bot._cycle()
            assert second["strategy_updated"]
            assert second["risk"]["allowed"]
            assert bot.broker.enter.await_args.args[2] == 102.0
            assert bot.db.get_state("last_entry_signal_candle") == "123"
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())
