"""Tests for equity failure-reason observability in the orchestrator."""

import asyncio
from unittest.mock import AsyncMock

from app.agents.orchestrator import TradingOrchestrator
from app.config import Settings


def make_bot(tmp_path, mode="testnet", **overrides) -> TradingOrchestrator:
    settings = Settings(
        _env_file=None,
        mode=mode,
        binance_api_key="test-key",
        binance_api_secret="test-secret",
        enable_llm_advisor=False,
        enable_adaptive_learning=False,
        use_websocket_market_data=False,
        account_refresh_seconds=5.0,
        account_max_age_seconds=15.0,
        database_path=str(tmp_path / "trades.db"),
        learning_profile_path=str(tmp_path / "learning.json"),
        **overrides,
    )
    bot = TradingOrchestrator(settings)
    bot._exchange_validated = True
    return bot


def test_equity_failure_captures_credentials_reason(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        try:
            bot.broker.equity = AsyncMock(
                side_effect=RuntimeError("Binance API credentials are missing")
            )
            # First call schedules the background account request.
            first = await bot._poll_equity(100.0)
            assert first is None
            assert bot.last_equity_error == "Waiting for the first account refresh"
            # Let the scheduled account task run, then consume its failure.
            await asyncio.sleep(0)
            second = await bot._poll_equity(100.0)
            assert second is None
            assert bot.last_equity_error is not None
            assert "credentials" in bot.last_equity_error.lower()
            assert "API credentials are missing" in bot.last_equity_error
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())


def test_equity_success_clears_error_and_returns_value(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        try:
            bot.broker.equity = AsyncMock(return_value=1234.5)
            assert await bot._poll_equity(100.0) is None  # scheduled, not yet consumed
            await asyncio.sleep(0)  # let the scheduled account task finish
            value = await bot._poll_equity(100.0)
            assert value == 1234.5
            assert bot.last_equity_error is None
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())


def test_stale_account_data_reports_age_reason(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path)
        try:
            bot.broker.equity = AsyncMock(return_value=500.0)
            await bot._poll_equity(100.0)  # schedule
            await asyncio.sleep(0)
            await bot._poll_equity(100.0)  # consume; fresh now
            assert bot.last_equity_error is None
            # Simulate aging past the account max age.
            import time

            bot.last_equity_update_monotonic = time.monotonic() - 60.0
            value = await bot._poll_equity(100.0)
            assert value is None
            assert bot.last_equity_error is not None
            assert "60s old" in bot.last_equity_error
            assert "limit 15s" in bot.last_equity_error
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())


def test_paper_equity_failure_reports_reason(tmp_path):
    async def scenario():
        bot = make_bot(tmp_path, mode="paper")
        try:
            bot.broker.equity = AsyncMock(side_effect=RuntimeError("database is locked"))
            value = await bot._poll_equity(100.0)
            assert value is None
            assert bot.last_equity_error is not None
            assert "database is locked" in bot.last_equity_error
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())


def test_cycle_result_payload_includes_equity_error(tmp_path):
    """The last_cycle dict exposes the reason for the dashboard to display."""

    async def scenario():
        bot = make_bot(tmp_path, mode="paper")
        try:
            bot._market_price = AsyncMock(return_value=(100.0, "test", 0.0))
            result = await bot._cycle_unlocked()
            assert "equity_error" in result
            assert result["equity_error"] == bot.last_equity_error
        finally:
            await bot.exchange.close()

    asyncio.run(scenario())
