import asyncio
from unittest.mock import AsyncMock, patch

import check_live_readiness as readiness
from app.config import Settings
from app.storage.db import TradingDB


def _settings(tmp_path):
    return Settings(
        _env_file=None,
        mode="testnet",
        symbol="BTCUSDT",
        symbols="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        allow_live_trading=False,
        live_protection_validated=False,
        allow_multi_symbol_live=False,
        binance_api_key="key",
        binance_api_secret="secret",
        database_path=str(tmp_path / "readiness-oco.db"),
    )


def _open_protected_trade(db):
    trade_id = db.open_trade(
        mode="testnet",
        symbol="BTCUSDT",
        quantity=0.001,
        entry_price=100,
        entry_fee=0,
        reason="readiness-check",
        stop_price=95,
        take_profit_price=110,
    )
    db.set_trade_protection(
        trade_id,
        order_list_id=9000,
        list_client_order_id="agt-prot-readiness",
        status="EXECUTING",
    )


def test_readiness_requires_both_active_verified_oco_child_orders(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        db = TradingDB(settings.database_path)
        db.init()
        _open_protected_trade(db)

        exchange = AsyncMock()
        exchange.ping.return_value = True
        exchange.account.return_value = {"balances": []}
        exchange.open_orders.return_value = [
            {
                "symbol": "BTCUSDT",
                "orderId": 9001,
                "clientOrderId": "agt-tp-readiness",
                "side": "SELL",
                "type": "LIMIT_MAKER",
            },
            {
                "symbol": "BTCUSDT",
                "orderId": 9002,
                "clientOrderId": "agt-sl-readiness",
                "side": "SELL",
                "type": "STOP_LOSS",
            },
        ]
        exchange.get_order_list.return_value = {
            "symbol": "BTCUSDT",
            "orderListId": 9000,
            "listClientOrderId": "agt-prot-readiness",
            "listOrderStatus": "EXECUTING",
            "orders": [
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9001,
                    "clientOrderId": "agt-tp-readiness",
                },
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9002,
                    "clientOrderId": "agt-sl-readiness",
                },
            ],
        }

        async def child_order(symbol, *, client_order_id):
            is_take_profit = client_order_id == "agt-tp-readiness"
            return {
                "symbol": symbol,
                "orderId": 9001 if is_take_profit else 9002,
                "clientOrderId": client_order_id,
                "status": "NEW",
                "type": "LIMIT_MAKER" if is_take_profit else "STOP_LOSS",
                "side": "SELL",
                "executedQty": "0",
            }

        exchange.get_order = AsyncMock(side_effect=child_order)
        exchange.executed_quantity = lambda order: float(order.get("executedQty") or 0)
        exchange.close.return_value = None

        with patch.object(readiness, "Settings", return_value=settings), patch.object(
            readiness, "BinanceClient", return_value=exchange
        ):
            result = await readiness.run_readiness_check()

        assert result["ok"] is True
        checks = {item["check"]: item for item in result["checks"]}
        assert checks["BTCUSDT_exchange_protection_children_verified"]["ok"] is True
        assert checks["BTCUSDT_exchange_protection_both_legs_active"]["ok"] is True
        assert checks["BTCUSDT_open_orders_visible"]["ok"] is True
        exchange.get_order.assert_awaited()
        await exchange.close()

    asyncio.run(scenario())


def test_readiness_rejects_incomplete_oco_children_for_open_position(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        db = TradingDB(settings.database_path)
        db.init()
        _open_protected_trade(db)

        exchange = AsyncMock()
        exchange.ping.return_value = True
        exchange.account.return_value = {"balances": []}
        exchange.open_orders.return_value = []
        exchange.get_order_list.return_value = {
            "symbol": "BTCUSDT",
            "orderListId": 9000,
            "listClientOrderId": "agt-prot-readiness",
            "listOrderStatus": "EXECUTING",
            "orders": [
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9002,
                    "clientOrderId": "agt-sl-readiness",
                }
            ],
        }
        exchange.close.return_value = None

        with patch.object(readiness, "Settings", return_value=settings), patch.object(
            readiness, "BinanceClient", return_value=exchange
        ):
            result = await readiness.run_readiness_check()

        checks = {item["check"]: item for item in result["checks"]}
        assert result["ok"] is False
        assert checks["BTCUSDT_exchange_protection_query"]["ok"] is False
        exchange.get_order.assert_not_awaited()
        await exchange.close()

    asyncio.run(scenario())
