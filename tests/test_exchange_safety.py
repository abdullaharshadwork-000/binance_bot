import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

from app.config import Settings
from app.exchange.binance import BinanceClient
from app.execution.broker import Broker
from app.models import RiskDecision, SignalSide, StrategySignal
from app.runtime_lock import RuntimeFileLock
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


def test_paper_base_balances_are_isolated_by_symbol(tmp_path):
    db = TradingDB(str(tmp_path / "paper.db"))
    db.init()
    btc_settings = Settings(
        _env_file=None,
        mode="paper",
        symbol="BTCUSDT",
        quote_asset="USDT",
        database_path=db.path,
    )
    eth_settings = Settings(
        _env_file=None,
        mode="paper",
        symbol="ETHUSDT",
        quote_asset="USDT",
        database_path=db.path,
    )
    btc = Broker(btc_settings, BinanceClient(btc_settings), db)
    eth = Broker(eth_settings, BinanceClient(eth_settings), db)

    db.set_state(btc._paper_base_key, 1.25)

    assert btc._paper_base_dec() == Decimal("1.25")
    assert eth._paper_base_dec() == Decimal("0.0")
    assert btc._paper_quote_key == eth._paper_quote_key


def test_legacy_paper_balance_is_migrated_only_to_primary_symbol(tmp_path):
    db = TradingDB(str(tmp_path / "legacy.db"))
    db.init()
    db.set_state("paper_quote_balance", "750.5")
    db.set_state("paper_base_balance", "0.25")

    btc_settings = Settings(_env_file=None, symbol="BTCUSDT", database_path=db.path)
    eth_settings = Settings(_env_file=None, symbol="ETHUSDT", database_path=db.path)
    btc = Broker(btc_settings, BinanceClient(btc_settings), db)
    eth = Broker(eth_settings, BinanceClient(eth_settings), db)

    assert btc._paper_quote_dec() == Decimal("750.5")
    assert eth._paper_quote_dec() == Decimal("750.5")
    assert btc._paper_base_dec() == Decimal("0.25")
    assert eth._paper_base_dec() == Decimal("0.0")


def test_disabled_market_step_does_not_discard_market_maximum():
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        client.symbol_filters = AsyncMock(return_value={
            "LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
            "MARKET_LOT_SIZE": {"stepSize": "0", "minQty": "0", "maxQty": "1"}})
        assert await client.normalize_quantity_decimal("BTCUSDT", Decimal("2"), Decimal("100")) == 0
        assert await client.normalize_quantity_decimal("BTCUSDT", Decimal("0.1234"), Decimal("100")) == Decimal("0.123")
    asyncio.run(scenario())


def test_market_and_regular_lot_steps_are_both_respected():
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        client.symbol_filters = AsyncMock(return_value={
            "LOT_SIZE": {"stepSize": "0.003"}, "MARKET_LOT_SIZE": {"stepSize": "0.002"}})
        assert await client.normalize_quantity_decimal("BTCUSDT", Decimal("0.011"), Decimal("100")) == Decimal("0.006")
    asyncio.run(scenario())


def test_market_notional_application_flags_are_respected():
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        client.symbol_filters = AsyncMock(return_value={"LOT_SIZE": {"stepSize": "1"},
            "NOTIONAL": {"minNotional": "500", "maxNotional": "10", "applyMinToMarket": False, "applyMaxToMarket": False}})
        assert await client.normalize_quantity_decimal("BTCUSDT", Decimal("1"), Decimal("100")) == 1
    asyncio.run(scenario())


def test_nonfinite_quantity_is_rejected_before_filter_request():
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        client.symbol_filters = AsyncMock()
        for value in ["NaN", "Infinity", "-1", "0"]:
            assert await client.normalize_quantity_decimal("BTCUSDT", Decimal(value), Decimal("100")) == 0
        client.symbol_filters.assert_not_awaited()
    asyncio.run(scenario())


def test_exchange_client_cannot_bypass_live_or_paper_order_gate():
    import pytest
    async def scenario():
        for mode in ["paper", "live"]:
            client = BinanceClient(Settings(_env_file=None, mode=mode,
                binance_api_key="test", binance_api_secret="test", allow_live_trading=False))
            client._request = AsyncMock()
            with pytest.raises(RuntimeError, match="disabled"):
                await client.market_order("BTCUSDT", "BUY", 1)
            client._request.assert_not_awaited()
    asyncio.run(scenario())


def test_halted_symbol_cannot_receive_market_order():
    import pytest
    async def scenario():
        client = BinanceClient(Settings(_env_file=None, mode="testnet", binance_api_key="test", binance_api_secret="test"))
        client.exchange_info = AsyncMock(return_value={"status": "HALT", "isSpotTradingAllowed": True, "orderTypes": ["MARKET"]})
        client._request = AsyncMock()
        with pytest.raises(RuntimeError, match="not available"):
            await client.market_order("BTCUSDT", "BUY", 1)
        client._request.assert_not_awaited()
    asyncio.run(scenario())


def test_rate_limit_cooldown_is_shared_between_symbol_clients():
    import httpx
    import pytest
    async def scenario():
        first = BinanceClient(Settings(_env_file=None))
        second = BinanceClient(Settings(_env_file=None))
        first._client = httpx.AsyncClient(base_url=first.base_url, transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "120"}, json={"msg": "rate limited"})))
        second._client = httpx.AsyncClient(base_url=first.base_url, transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("Network must not be used during cooldown"))))
        try:
            with pytest.raises(RuntimeError, match="429"): await first.ping()
            with pytest.raises(RuntimeError, match="cooldown"): await second.ping()
        finally:
            BinanceClient._blocked_until.clear()
            await first.close()
            await second.close()
    asyncio.run(scenario())


def test_entry_depth_estimates_weighted_fill():
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        client._request = AsyncMock(return_value={"bids": [["99.99", "10"]],
            "asks": [["100.01", "1"], ["100.03", "2"]]})
        result = await client.check_entry_liquidity("BTCUSDT", 2, 100)
        assert abs(result["estimated_fill_price"] - 100.02) < 1e-9
    asyncio.run(scenario())


def test_bad_entry_liquidity_is_rejected():
    import pytest
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        books = [
            {"bids": [], "asks": []},
            {"bids": [["99", "10"]], "asks": [["101", "10"]]},
            {"bids": [["99.99", "1"]], "asks": [["100.01", ".1"]]},
            {"bids": [["101", "10"]], "asks": [["101.01", "10"]]},
            {"bids": [["100.02", "10"]], "asks": [["100.01", "10"]]},
            {"bids": [["99.99", "10"]], "asks": [["NaN", "10"]]},
            {"bids": [["99.99", "10"]], "asks": [["100.02", "1"], ["100.01", "2"]]},
        ]
        for book in books:
            client._request = AsyncMock(return_value=book)
            with pytest.raises(ValueError):
                await client.check_entry_liquidity("BTCUSDT", 1, 100)
    asyncio.run(scenario())


def test_database_enforces_only_one_open_trade_per_mode_symbol(tmp_path):
    import sqlite3
    import pytest

    db = TradingDB(str(tmp_path / "unique.db"))
    db.init()
    db.open_trade(
        mode="paper",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="first",
        stop_price=95,
        take_profit_price=110,
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.open_trade(
            mode="paper",
            symbol="BTCUSDT",
            quantity=1,
            entry_price=100,
            entry_fee=0,
            reason="duplicate",
            stop_price=95,
            take_profit_price=110,
        )


def test_order_update_requires_existing_order_row(tmp_path):
    import pytest

    db = TradingDB(str(tmp_path / "order-update.db"))
    db.init()
    with pytest.raises(ValueError, match="not updated exactly once"):
        db.update_order_record(
            client_order_id="missing",
            binance_order_id=None,
            executed_quantity=0,
            average_fill_price=None,
            status="UNKNOWN",
            commission_quote=0,
            commission_details=[],
        )


def test_runtime_lock_prevents_second_process_owner_for_same_database(tmp_path):
    import pytest

    database = str(tmp_path / "shared.db")
    first = RuntimeFileLock(database)
    second = RuntimeFileLock(database)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="Another trading-bot process"):
            second.acquire()
    finally:
        first.release()


def test_live_order_execution_is_blocked_until_exchange_protection_exists():
    import pytest

    with pytest.raises(ValueError, match="exchange-resident"):
        Settings(
            _env_file=None,
            mode="live",
            allow_live_trading=True,
            binance_api_key="test",
            binance_api_secret="test",
        )
