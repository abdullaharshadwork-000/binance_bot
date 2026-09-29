import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import cleanup_testnet_balances as cleanup
from app.config import Settings
from app.storage.db import TradingDB


def make_settings(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "mode": "testnet",
        "symbol": "BTCUSDT",
        "symbols": "BTCUSDT,ETHUSDT",
        "quote_asset": "USDT",
        "binance_api_key": "key",
        "binance_api_secret": "secret",
        "allow_live_trading": False,
        "live_protection_validated": False,
        "allow_multi_symbol_live": False,
        "database_path": str(tmp_path / "cleanup.db"),
    }
    values.update(overrides)
    return Settings(**values)


def test_cleanup_plan_refuses_live_mode(tmp_path):
    async def scenario():
        settings = make_settings(
            tmp_path,
            mode="live",
            allow_live_trading=True,
            live_protection_validated=True,
            allow_multi_symbol_live=True,
        )
        db = TradingDB(settings.database_path)
        db.init()
        with pytest.raises(RuntimeError, match="MODE must be testnet"):
            await cleanup.build_cleanup_plan(settings, db)

    asyncio.run(scenario())


def test_cleanup_plan_refuses_local_open_position(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path)
        db = TradingDB(settings.database_path)
        db.init()
        db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.01,
            entry_price=100.0,
            entry_fee=0.0,
            reason="managed",
            stop_price=95.0,
            take_profit_price=110.0,
        )
        with pytest.raises(RuntimeError, match="local open position exists"):
            await cleanup.build_cleanup_plan(settings, db)

    asyncio.run(scenario())


def test_cleanup_plan_builds_normalized_testnet_sell_plan(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path)
        db = TradingDB(settings.database_path)
        db.init()

        primary = AsyncMock()
        eth_client = AsyncMock()

        primary.open_orders.return_value = []
        primary.asset_balance_details.return_value = {
            "free": 1.00029,
            "locked": 0.0,
            "total": 1.00029,
        }
        primary.ticker_price.return_value = 84000.0
        primary.normalize_quantity_decimal.return_value = __import__("decimal").Decimal("1.0002")

        eth_client.asset_balance_details.return_value = {
            "free": 1.0,
            "locked": 0.0,
            "total": 1.0,
        }
        eth_client.ticker_price.return_value = 2700.0
        eth_client.normalize_quantity_decimal.return_value = __import__("decimal").Decimal("1.0")

        with patch.object(cleanup, "BinanceClient", side_effect=[primary, eth_client]):
            plan, clients = await cleanup.build_cleanup_plan(settings, db)

        assert [item["symbol"] for item in plan] == ["BTCUSDT", "ETHUSDT"]
        assert plan[0]["status"] == "READY"
        assert plan[0]["sell_quantity"] == 1.0002
        assert plan[1]["status"] == "READY"
        assert plan[1]["sell_quantity"] == 1.0
        assert clients == [primary, eth_client]
        primary.open_orders.assert_awaited_once()

    asyncio.run(scenario())


def test_cleanup_plan_refuses_locked_balance(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path, symbols="BTCUSDT")
        db = TradingDB(settings.database_path)
        db.init()

        client = AsyncMock()
        client.open_orders.return_value = []
        client.asset_balance_details.return_value = {
            "free": 0.5,
            "locked": 0.5,
            "total": 1.0,
        }

        with patch.object(cleanup, "BinanceClient", return_value=client):
            with pytest.raises(RuntimeError, match="locked balance"):
                await cleanup.build_cleanup_plan(settings, db)

    asyncio.run(scenario())


def test_execute_cleanup_reconciles_submit_timeout_by_client_id(monkeypatch):
    async def scenario():
        client = AsyncMock()
        client.market_order.side_effect = TimeoutError("timeout")
        client.get_order.return_value = {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "clientOrderId": "fixed-cleanup-id",
            "orderId": 55,
            "status": "FILLED",
            "executedQty": "0.5",
            "cummulativeQuoteQty": "42000",
        }
        client.executed_quantity.return_value = 0.5
        client.weighted_fill_price.return_value = 84000.0

        monkeypatch.setattr(
            cleanup.uuid,
            "uuid4",
            lambda: type("U", (), {"hex": "fixed-cleanup-id-extra"})(),
        )
        plan = [{
            "symbol": "BTCUSDT",
            "asset": "BTC",
            "available": 0.5,
            "sell_quantity": 0.5,
            "estimated_quote": 42000.0,
            "status": "READY",
            "client": client,
        }]

        # Match the exact deterministic id produced by the cleanup helper.
        client.get_order.return_value["clientOrderId"] = "agt-clean-fixed-cleanup-id-ext"

        result = await cleanup.execute_cleanup(plan)

        assert result[0]["status"] == "FILLED"
        assert result[0]["executed_quantity"] == 0.5
        client.market_order.assert_awaited_once()
        client.get_order.assert_awaited_once_with(
            "BTCUSDT",
            client_order_id="agt-clean-fixed-cleanup-id-ext",
        )

    asyncio.run(scenario())
