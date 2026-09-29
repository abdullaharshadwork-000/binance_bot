import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import validate_testnet_protection as validator
from app.config import Settings
from app.exchange.binance import BinanceClient


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
