import asyncio
import time
from unittest.mock import AsyncMock, patch

from app.agents.multi_orchestrator import MultiSymbolTradingManager
from app.config import Settings
from app.storage.db import TradingDB


async def tick(bot, price):
    await bot._on_price_tick(price, int(time.time() * 1000))


def make_manager(tmp_path):
    settings = Settings(
        _env_file=None,
        mode="testnet",
        symbols="BTCUSDT,ETHUSDT",
        binance_api_key="test",
        binance_api_secret="test",
        database_path=str(tmp_path / "portfolio.db"),
        learning_profile_path=str(tmp_path / "profile.json"),
    )
    return MultiSymbolTradingManager(settings)


def test_universe_external_account_assets_are_priced_and_count_toward_exposure(tmp_path):
    async def scenario():
        manager = make_manager(tmp_path)
        await tick(manager.get_bot("BTCUSDT"), 100)
        portfolio = manager.portfolio
        exchange = manager.primary_bot.exchange
        exchange.account = AsyncMock(return_value={"balances": [
            {"asset": "USDT", "free": "500", "locked": "0"},
            {"asset": "BTC", "free": "1", "locked": "0"},
            {"asset": "BNB", "free": "2", "locked": "0"},
            {"asset": "TRY", "free": "1000", "locked": "0"},
        ]})
        exchange.asset_quote_conversion_pairs = AsyncMock(return_value={
            "BNB": {"symbol": "BNBUSDT", "inverse": False},
            "TRY": {"symbol": "USDTTRY", "inverse": True},
        })
        exchange.ticker_prices = AsyncMock(return_value={
            "BNBUSDT": 300,
            "USDTTRY": 20,
        })

        assert await portfolio.poll_equity() is None
        await portfolio._balance_task
        assert await portfolio.poll_equity() is None  # External asset quote prices are pending.
        await portfolio._valuation_task
        assert await portfolio.poll_equity() == 1250

        snapshot = portfolio.snapshot()
        assert snapshot["exposure"] == 750
        assert snapshot["managed_exposure"] == 0
        assert snapshot["unmanaged_exposure"] == 750
        assert {item["asset"] for item in snapshot["other_account_assets"]} == {"BNB", "TRY"}
        assert snapshot["available_quote"] == 500
        await portfolio.close()
        for bot in manager.bots.values():
            await bot.exchange.close()

    asyncio.run(scenario())


def test_unpriceable_nonuniverse_asset_blocks_new_entries_fail_closed(tmp_path):
    async def scenario():
        manager = make_manager(tmp_path)
        await tick(manager.get_bot("BTCUSDT"), 100)
        portfolio = manager.portfolio
        exchange = manager.primary_bot.exchange
        exchange.account = AsyncMock(return_value={"balances": [
            {"asset": "USDT", "free": "500", "locked": "0"},
            {"asset": "BTC", "free": "1", "locked": "0"},
            {"asset": "UNLISTED", "free": "3", "locked": "0"},
        ]})
        exchange.asset_quote_conversion_pairs = AsyncMock(return_value={})

        assert await portfolio.poll_equity() is None
        await portfolio._balance_task
        assert await portfolio.poll_equity() is None
        await portfolio._valuation_task
        assert await portfolio.poll_equity() is None
        snapshot = portfolio.snapshot()
        assert "UNLISTED" in snapshot["error"]
        submit = AsyncMock()
        result = await portfolio.execute_entry("BTCUSDT", submit, notional=10)
        assert not result.success
        assert "UNLISTED" in result.message
        submit.assert_not_awaited()
        await portfolio.close()
        for bot in manager.bots.values():
            await bot.exchange.close()

    asyncio.run(scenario())


def test_live_readiness_reports_other_account_assets_as_unpriced_or_valued(tmp_path):
    import check_live_readiness as readiness

    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbols="BTCUSDT",
            allow_live_trading=False,
            live_protection_validated=False,
            allow_multi_symbol_live=False,
            binance_api_key="test",
            binance_api_secret="test",
            database_path=str(tmp_path / "readiness-assets.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        exchange = AsyncMock()
        exchange.ping.return_value = True
        exchange.account.return_value = {"balances": [
            {"asset": "USDT", "free": "500", "locked": "0"},
            {"asset": "BTC", "free": "1", "locked": "0"},
            {"asset": "BNB", "free": "2", "locked": "0"},
        ]}
        exchange.open_orders.return_value = []
        exchange.asset_quote_conversion_pairs = AsyncMock(return_value={
            "BNB": {"symbol": "BNBUSDT", "inverse": False},
        })
        exchange.ticker_prices = AsyncMock(return_value={"BNBUSDT": 300})

        with patch.object(readiness, "Settings", return_value=settings), patch.object(
            readiness, "BinanceClient", return_value=exchange
        ):
            result = await readiness.run_readiness_check()

        valuation = next(
            item for item in result["checks"]
            if item["check"] == "other_account_assets_valued"
        )
        assert valuation["ok"] is True
        assert "BNB=600" in valuation["detail"]
        await exchange.close()

        exchange = AsyncMock()
        exchange.ping.return_value = True
        exchange.account.return_value = {"balances": [
            {"asset": "USDT", "free": "500", "locked": "0"},
            {"asset": "UNLISTED", "free": "1", "locked": "0"},
        ]}
        exchange.open_orders.return_value = []
        exchange.asset_quote_conversion_pairs = AsyncMock(return_value={})
        with patch.object(readiness, "Settings", return_value=settings), patch.object(
            readiness, "BinanceClient", return_value=exchange
        ):
            result = await readiness.run_readiness_check()
        valuation = next(
            item for item in result["checks"]
            if item["check"] == "other_account_assets_valued"
        )
        assert valuation["ok"] is False
        assert "UNLISTED" in valuation["detail"]
        await exchange.close()

    asyncio.run(scenario())
