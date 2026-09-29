import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import validate_testnet_protection as validator
from app.config import Settings
from app.exchange.binance import BinanceClient
from app.storage.db import TradingDB


def test_validation_settings_are_for_isolated_single_symbol_testnet(tmp_path):
    base = Settings(
        _env_file=None,
        mode="testnet",
        binance_api_key="key",
        binance_api_secret="secret",
        symbols="BTCUSDT,ETHUSDT",
    )
    settings = validator.build_validation_settings(
        base,
        str(tmp_path / "validation.db"),
    )

    assert settings.mode == "testnet"
    assert settings.trading_symbols == ["BTCUSDT"]
    assert settings.database_path.endswith("validation.db")
    assert settings.allow_live_trading is False


def test_validation_refuses_non_testnet_configuration(tmp_path):
    fake = Settings(_env_file=None, mode="paper")
    with patch.object(validator, "Settings", return_value=fake):
        with pytest.raises(RuntimeError, match="MODE=testnet"):
            asyncio.run(
                validator.run_validation(
                    25.0,
                    str(tmp_path / "validation.db"),
                )
            )


def test_validation_refuses_existing_exchange_orders(tmp_path):
    base = Settings(
        _env_file=None,
        mode="testnet",
        binance_api_key="key",
        binance_api_secret="secret",
        database_path=str(tmp_path / "main.db"),
    )

    exchange = AsyncMock()
    exchange.ping.return_value = True
    exchange.validate_symbol_assets.return_value = None
    exchange.open_orders.return_value = [{"orderId": 1}]
    exchange.close.return_value = None

    with patch.object(validator, "Settings", return_value=base), \
         patch.object(validator, "BinanceClient", return_value=exchange):
        with pytest.raises(RuntimeError, match="already has 1 open Binance order"):
            asyncio.run(
                validator.run_validation(
                    25.0,
                    str(tmp_path / "validation.db"),
                )
            )

    exchange.open_orders.assert_awaited_once_with("BTCUSDT")


def test_open_orders_queries_account_wide_then_filters_symbol():
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            binance_api_key="key",
            binance_api_secret="secret",
        )
        client = BinanceClient(settings)
        client._request = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "orderId": 1},
            {"symbol": "ETHUSDT", "orderId": 2},
        ])

        orders = await client.open_orders("BTCUSDT")

        assert orders == [{"symbol": "BTCUSDT", "orderId": 1}]
        client._request.assert_awaited_once_with(
            "GET",
            "/api/v3/openOrders",
            signed=True,
        )

    asyncio.run(scenario())


def test_resume_validation_returns_clean_when_no_local_position(tmp_path):
    base = Settings(
        _env_file=None,
        mode="testnet",
        binance_api_key="key",
        binance_api_secret="secret",
        database_path=str(tmp_path / "main.db"),
    )
    with patch.object(validator, "Settings", return_value=base):
        result = asyncio.run(
            validator.resume_validation(str(tmp_path / "validation.db"))
        )

    assert result["ok"] is True
    assert result["recovered"] is True
    assert "No open validation position" in result["message"]


def test_resume_validation_reconciles_and_closes_existing_position(tmp_path):
    base = Settings(
        _env_file=None,
        mode="testnet",
        binance_api_key="key",
        binance_api_secret="secret",
        database_path=str(tmp_path / "main.db"),
    )
    validation_db = str(tmp_path / "validation.db")
    db = TradingDB(validation_db)
    db.init()
    db.open_trade(
        mode="testnet",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="validation",
        stop_price=95,
        take_profit_price=110,
    )

    exchange = AsyncMock()
    exchange.ping.return_value = True
    exchange.validate_symbol_assets.return_value = None
    exchange.ticker_price.return_value = 100
    exchange.close.return_value = None

    broker = AsyncMock()
    calls = {"n": 0}

    async def maybe_exit(signal, price):
        calls["n"] += 1
        if calls["n"] == 1:
            return type(
                "R",
                (),
                {"action": "HOLD", "message": "protected", "success": True},
            )()

        trade = db.get_open_trade("BTCUSDT", "testnet")
        db.close_trade(
            int(trade["id"]),
            100,
            0,
            "validation cleanup",
            executed_quantity=float(trade["quantity"]),
        )
        return type(
            "R",
            (),
            {"action": "SELL", "message": "closed", "success": True},
        )()

    broker.maybe_exit.side_effect = maybe_exit

    validation_settings = validator.build_validation_settings(
        base,
        validation_db,
    )

    with patch.object(
        validator,
        "Settings",
        side_effect=[base, validation_settings],
    ), patch.object(
        validator,
        "BinanceClient",
        return_value=exchange,
    ), patch.object(
        validator,
        "Broker",
        return_value=broker,
    ):
        result = asyncio.run(
            validator.resume_validation(validation_db)
        )

    assert result["ok"] is True
    assert result["recovered"] is True
    assert result["cleanup"]["action"] == "SELL"



def test_recover_and_close_verifies_same_persisted_oco(tmp_path):
    base = Settings(
        _env_file=None,
        mode="testnet",
        binance_api_key="key",
        binance_api_secret="secret",
        database_path=str(tmp_path / "main.db"),
    )
    validation_db = str(tmp_path / "validation.db")
    db = TradingDB(validation_db)
    db.init()
    trade_id = db.open_trade(
        mode="testnet",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="restart-validation",
        stop_price=95,
        take_profit_price=110,
    )
    db.set_trade_protection(
        trade_id,
        order_list_id=777,
        list_client_order_id="agt-prot-restart",
        status="EXECUTING",
    )

    exchange = AsyncMock()
    exchange.ping.return_value = True
    exchange.validate_symbol_assets.return_value = None
    exchange.ticker_price.return_value = 100
    exchange.get_order_list.return_value = {
        "symbol": "BTCUSDT",
        "orderListId": 777,
        "listClientOrderId": "agt-prot-restart",
        "listOrderStatus": "EXECUTING",
        "orders": [],
    }
    exchange.close.return_value = None

    broker = AsyncMock()
    calls = {"n": 0}

    async def maybe_exit(signal, price):
        calls["n"] += 1
        if calls["n"] == 1:
            return type(
                "R",
                (),
                {"action": "HOLD", "message": "Open position retained", "success": True},
            )()

        trade = db.get_open_trade("BTCUSDT", "testnet")
        db.close_trade(
            int(trade["id"]),
            100,
            0,
            "restart validation cleanup",
            executed_quantity=float(trade["quantity"]),
        )
        return type(
            "R",
            (),
            {"action": "SELL", "message": "closed", "success": True},
        )()

    broker.maybe_exit.side_effect = maybe_exit
    validation_settings = validator.build_validation_settings(base, validation_db)

    with patch.object(
        validator,
        "Settings",
        side_effect=[base, validation_settings],
    ), patch.object(
        validator,
        "BinanceClient",
        return_value=exchange,
    ), patch.object(
        validator,
        "Broker",
        return_value=broker,
    ):
        result = asyncio.run(
            validator.recover_and_close_validation(validation_db)
        )

    assert result["ok"] is True
    assert result["same_oco_verified"] is True
    assert result["protective_order_list_id"] == "777"
    assert result["cleanup"]["action"] == "SELL"
    exchange.get_order_list.assert_awaited_once_with(
        "BTCUSDT",
        list_client_order_id="agt-prot-restart",
    )


def test_recover_and_close_fails_on_different_oco_identity(tmp_path):
    base = Settings(
        _env_file=None,
        mode="testnet",
        binance_api_key="key",
        binance_api_secret="secret",
        database_path=str(tmp_path / "main.db"),
    )
    validation_db = str(tmp_path / "validation.db")
    db = TradingDB(validation_db)
    db.init()
    trade_id = db.open_trade(
        mode="testnet",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="restart-validation",
        stop_price=95,
        take_profit_price=110,
    )
    db.set_trade_protection(
        trade_id,
        order_list_id=777,
        list_client_order_id="agt-prot-restart",
        status="EXECUTING",
    )

    exchange = AsyncMock()
    exchange.ping.return_value = True
    exchange.validate_symbol_assets.return_value = None
    exchange.get_order_list.return_value = {
        "symbol": "BTCUSDT",
        "orderListId": 999,
        "listClientOrderId": "agt-prot-other",
        "listOrderStatus": "EXECUTING",
        "orders": [],
    }
    exchange.close.return_value = None

    validation_settings = validator.build_validation_settings(base, validation_db)

    with patch.object(
        validator,
        "Settings",
        side_effect=[base, validation_settings],
    ), patch.object(
        validator,
        "BinanceClient",
        return_value=exchange,
    ):
        with pytest.raises(RuntimeError, match="different OCO client id"):
            asyncio.run(
                validator.recover_and_close_validation(validation_db)
            )

    assert db.get_open_trade("BTCUSDT", "testnet") is not None



def test_offline_recovery_refuses_still_executing_oco(tmp_path):
    base = Settings(
        _env_file=None,
        mode="testnet",
        binance_api_key="key",
        binance_api_secret="secret",
        database_path=str(tmp_path / "main.db"),
    )
    validation_db = str(tmp_path / "validation.db")
    db = TradingDB(validation_db)
    db.init()
    trade_id = db.open_trade(
        mode="testnet",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="offline-fill",
        stop_price=99,
        take_profit_price=101,
    )
    db.set_trade_protection(
        trade_id,
        order_list_id=888,
        list_client_order_id="agt-prot-offline",
        status="EXECUTING",
    )

    exchange = AsyncMock()
    exchange.ping.return_value = True
    exchange.get_order_list.return_value = {
        "symbol": "BTCUSDT",
        "orderListId": 888,
        "listClientOrderId": "agt-prot-offline",
        "listOrderStatus": "EXECUTING",
        "orders": [],
    }
    exchange.close.return_value = None
    validation_settings = validator.build_validation_settings(base, validation_db)

    with patch.object(
        validator,
        "Settings",
        side_effect=[base, validation_settings],
    ), patch.object(
        validator,
        "BinanceClient",
        return_value=exchange,
    ):
        with pytest.raises(RuntimeError, match="still EXECUTING"):
            asyncio.run(
                validator.recover_offline_fill_validation(validation_db)
            )

    assert db.get_open_trade("BTCUSDT", "testnet") is not None


def test_offline_recovery_uses_exchange_fill_without_new_bot_sell(tmp_path):
    base = Settings(
        _env_file=None,
        mode="testnet",
        binance_api_key="key",
        binance_api_secret="secret",
        database_path=str(tmp_path / "main.db"),
    )
    validation_db = str(tmp_path / "validation.db")
    db = TradingDB(validation_db)
    db.init()
    trade_id = db.open_trade(
        mode="testnet",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="offline-fill",
        stop_price=99,
        take_profit_price=101,
    )
    db.set_trade_protection(
        trade_id,
        order_list_id=888,
        list_client_order_id="agt-prot-offline",
        status="EXECUTING",
    )

    exchange = AsyncMock()
    exchange.ping.return_value = True
    exchange.ticker_price.return_value = 101
    exchange.get_order_list.return_value = {
        "symbol": "BTCUSDT",
        "orderListId": 888,
        "listClientOrderId": "agt-prot-offline",
        "listOrderStatus": "ALL_DONE",
        "orders": [],
    }
    exchange.close.return_value = None

    broker = AsyncMock()

    async def maybe_exit(signal, price):
        trade = db.get_open_trade("BTCUSDT", "testnet")
        db.close_trade(
            int(trade["id"]),
            101,
            0,
            "Exchange take profit",
            executed_quantity=float(trade["quantity"]),
        )
        return type(
            "R",
            (),
            {
                "action": "SELL",
                "message": "Exchange take profit",
                "success": True,
                "details": {"reason": "Exchange take profit"},
            },
        )()

    broker.maybe_exit.side_effect = maybe_exit
    validation_settings = validator.build_validation_settings(base, validation_db)

    with patch.object(
        validator,
        "Settings",
        side_effect=[base, validation_settings],
    ), patch.object(
        validator,
        "BinanceClient",
        return_value=exchange,
    ), patch.object(
        validator,
        "Broker",
        return_value=broker,
    ):
        result = asyncio.run(
            validator.recover_offline_fill_validation(validation_db)
        )

    assert result["ok"] is True
    assert result["same_oco_verified"] is True
    assert result["new_bot_sell_orders_during_recovery"] == 0
    assert db.get_open_trade("BTCUSDT", "testnet") is None
