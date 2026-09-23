import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

from app.config import Settings
from app.exchange.binance import BinanceClient
from app.execution.broker import Broker
from app.models import RiskDecision, SignalSide, StrategySignal
from app.storage.db import TradingDB


def test_market_filter_max_quantity_is_enforced():
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        client.symbol_filters = AsyncMock(return_value={
            "MARKET_LOT_SIZE": {
                "minQty": "0.001",
                "maxQty": "1.000",
                "stepSize": "0.001",
            },
            "NOTIONAL": {
                "minNotional": "5",
                "maxNotional": "100000",
            },
        })

        qty = await client.normalize_quantity_decimal(
            "BTCUSDT",
            Decimal("2"),
            Decimal("50000"),
        )
        assert qty == Decimal("0")
        await client.close()

    asyncio.run(scenario())


def test_equity_balance_includes_locked_amounts():
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        client.account = AsyncMock(return_value={
            "balances": [
                {"asset": "USDT", "free": "80", "locked": "20"},
                {"asset": "BTC", "free": "0.01", "locked": "0.02"},
            ]
        })

        balances = await client.asset_balances({"USDT", "BTC"})
        assert balances["USDT"] == 100.0
        assert balances["BTC"] == 0.03
        await client.close()

    asyncio.run(scenario())


def test_zero_fee_fill_does_not_fall_back_to_configured_fee():
    client = BinanceClient(Settings(_env_file=None, trading_fee_bps=10))
    order = {
        "cummulativeQuoteQty": "100",
        "fills": [
            {
                "price": "100",
                "qty": "1",
                "commission": "0",
                "commissionAsset": "USDT",
            }
        ],
    }
    assert client.estimated_order_fee_quote(order, 100) == 0.0


def test_unresolved_order_blocks_new_entry(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="paper",
            database_path=str(tmp_path / "trades.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        exchange = BinanceClient(settings)
        broker = Broker(settings, exchange, db)

        db.record_order_intent(
            mode="paper",
            symbol="BTCUSDT",
            client_order_id="uncertain-1",
            side="BUY",
            requested_quantity=0.01,
        )

        result = await broker.enter(
            StrategySignal(SignalSide.BUY, 0.9, "test"),
            RiskDecision(True, "allowed", quantity=0.01),
            100.0,
        )

        assert result.success is False
        assert "unresolved" in result.message.lower()
        await exchange.close()

    asyncio.run(scenario())


def test_third_asset_commission_is_converted_to_quote():
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        client.ticker_price = AsyncMock(return_value=200.0)
        order = {
            "fills": [
                {
                    "price": "100",
                    "qty": "1",
                    "commission": "0.01",
                    "commissionAsset": "BNB",
                }
            ]
        }

        fee = await client.order_fee_quote(order, 100.0)
        assert fee == 2.0
        client.ticker_price.assert_awaited_with("BNBUSDT")
        await client.close()

    asyncio.run(scenario())
