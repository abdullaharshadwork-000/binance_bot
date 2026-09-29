import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

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


def test_live_order_execution_requires_validated_exchange_protection():
    import pytest

    with pytest.raises(ValueError, match="LIVE_PROTECTION_VALIDATED"):
        Settings(
            _env_file=None,
            mode="live",
            allow_live_trading=True,
            binance_api_key="test",
            binance_api_secret="test",
        )


def test_live_oco_client_uses_same_spot_order_list_path():
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="live",
            allow_live_trading=True,
            live_protection_validated=True,
            binance_api_key="test",
            binance_api_secret="test",
        )
        client = BinanceClient(settings)
        client._request = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 77,
            "listClientOrderId": "live-protection",
            "listStatusType": "EXEC_STARTED",
            "listOrderStatus": "EXECUTING",
            "orders": [],
        })

        result = await client.place_protective_oco(
            "BTCUSDT",
            quantity=0.01,
            take_profit_price=110,
            stop_price=95,
            list_client_order_id="live-protection",
            take_profit_client_order_id="live-tp",
            stop_client_order_id="live-sl",
        )

        assert result["orderListId"] == 77
        args = client._request.await_args.args
        assert args[0] == "POST"
        assert args[1] == "/api/v3/orderList/oco"
        assert client.base_url == "https://api.binance.com"

    asyncio.run(scenario())


def test_order_list_query_omits_symbol_parameter():
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            binance_api_key="key",
            binance_api_secret="secret",
        )
        client = BinanceClient(settings)
        client._request = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 1,
            "listClientOrderId": "prot-1",
            "listOrderStatus": "EXECUTING",
            "orders": [],
        })

        await client.get_order_list(
            "BTCUSDT",
            list_client_order_id="prot-1",
        )

        client._request.assert_awaited_once_with(
            "GET",
            "/api/v3/orderList",
            {"origClientOrderId": "prot-1"},
            signed=True,
        )

    asyncio.run(scenario())



def test_offline_stop_loss_fill_reconciles_without_duplicate_market_sell(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "stop-recovery.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        trade_id = db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.0003,
            entry_price=83000.0,
            entry_fee=0.0,
            reason="offline-stop-test",
            stop_price=82917.0,
            take_profit_price=84000.0,
        )
        db.set_trade_protection(
            trade_id,
            order_list_id=9001,
            list_client_order_id="agt-prot-stop-fixture",
            status="EXECUTING",
        )

        exchange = BinanceClient(settings)
        exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 9001,
            "listClientOrderId": "agt-prot-stop-fixture",
            "listOrderStatus": "ALL_DONE",
            "orders": [
                {
                    "symbol": "BTCUSDT",
                    "orderId": 7001,
                    "clientOrderId": "agt-tp-stop-fixture",
                },
                {
                    "symbol": "BTCUSDT",
                    "orderId": 7002,
                    "clientOrderId": "agt-sl-stop-fixture",
                },
            ],
        })

        async def get_order(symbol, *, client_order_id=None, order_id=None):
            if client_order_id == "agt-tp-stop-fixture":
                return {
                    "symbol": "BTCUSDT",
                    "orderId": 7001,
                    "clientOrderId": "agt-tp-stop-fixture",
                    "status": "CANCELED",
                    "type": "LIMIT_MAKER",
                    "side": "SELL",
                    "price": "84000.00000000",
                    "origQty": "0.00030000",
                    "executedQty": "0.00000000",
                    "cummulativeQuoteQty": "0.00000000",
                }
            if client_order_id == "agt-sl-stop-fixture":
                return {
                    "symbol": "BTCUSDT",
                    "orderId": 7002,
                    "clientOrderId": "agt-sl-stop-fixture",
                    "status": "FILLED",
                    "type": "STOP_LOSS",
                    "side": "SELL",
                    "price": "0.00000000",
                    "stopPrice": "82917.00000000",
                    "origQty": "0.00030000",
                    "executedQty": "0.00030000",
                    "cummulativeQuoteQty": "24.87000000",
                }
            raise AssertionError(f"Unexpected order lookup: {client_order_id}")

        exchange.get_order = AsyncMock(side_effect=get_order)
        exchange.my_trades = AsyncMock(return_value=[
            {
                "symbol": "BTCUSDT",
                "orderId": 7002,
                "price": "82900.00000000",
                "qty": "0.00030000",
                "commission": "0.00000000",
                "commissionAsset": "USDT",
            }
        ])
        exchange.market_order = AsyncMock(
            side_effect=AssertionError(
                "Recovery must not submit a duplicate market SELL after OCO stop fill"
            )
        )

        broker = Broker(settings, exchange, db)
        result = await broker.maybe_exit(
            StrategySignal(
                SignalSide.HOLD,
                0.0,
                "offline stop recovery",
            ),
            82900.0,
        )

        assert result.success is True
        assert result.action == "SELL"
        assert result.message == "Exchange stop loss"
        assert result.details["reason"] == "Exchange stop loss"
        assert result.details["closed"]["partial"] is False
        assert result.details["closed"]["executed_quantity"] == 0.0003
        assert abs(result.details["fill_price"] - 82900.0) < 1e-12
        assert db.get_open_trade("BTCUSDT", "testnet") is None
        exchange.market_order.assert_not_awaited()
        exchange.my_trades.assert_awaited_once_with("BTCUSDT", order_id=7002)
        await exchange.close()

    asyncio.run(scenario())



def test_partial_protective_fill_reduces_local_position_once_and_keeps_remaining_protected(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "partial-protection.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        trade_id = db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.0004,
            entry_price=83000.0,
            entry_fee=0.04,
            reason="partial-test",
            stop_price=82000.0,
            take_profit_price=84000.0,
        )
        db.set_trade_protection(
            trade_id,
            order_list_id=9100,
            list_client_order_id="agt-prot-partial",
            status="EXECUTING",
        )

        exchange = BinanceClient(settings)
        exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 9100,
            "listClientOrderId": "agt-prot-partial",
            "listOrderStatus": "EXECUTING",
            "orders": [
                {
                    "orderId": 7100,
                    "clientOrderId": "agt-sl-partial",
                }
            ],
        })
        exchange.get_order = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderId": 7100,
            "clientOrderId": "agt-sl-partial",
            "status": "PARTIALLY_FILLED",
            "type": "STOP_LOSS",
            "side": "SELL",
            "origQty": "0.00040000",
            "executedQty": "0.00010000",
            "price": "0.00000000",
        })
        exchange.my_trades = AsyncMock(return_value=[
            {
                "symbol": "BTCUSDT",
                "orderId": 7100,
                "price": "81950.00000000",
                "qty": "0.00010000",
                "commission": "0.00100000",
                "commissionAsset": "USDT",
            }
        ])
        exchange.market_order = AsyncMock(
            side_effect=AssertionError("Partial protective fill must not cause a duplicate SELL")
        )

        broker = Broker(settings, exchange, db)
        first = await broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0.0, "partial reconciliation"),
            81950.0,
        )

        assert first.success is True
        assert first.action == "SELL"
        assert first.message == "Exchange stop loss"
        assert first.details["closed"]["partial"] is True
        assert first.details["closed"]["executed_quantity"] == 0.0001
        remaining = db.get_open_trade("BTCUSDT", "testnet")
        assert remaining is not None
        assert abs(float(remaining["quantity"]) - 0.0003) < 1e-12
        assert abs(float(remaining["protective_applied_qty"]) - 0.0001) < 1e-12

        second = await broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0.0, "repeat reconciliation"),
            81950.0,
        )

        assert second.success is True
        assert second.action == "HOLD"
        assert "partially filled" in second.message.lower()
        remaining_again = db.get_open_trade("BTCUSDT", "testnet")
        assert abs(float(remaining_again["quantity"]) - 0.0003) < 1e-12
        exchange.market_order.assert_not_awaited()
        await exchange.close()

    asyncio.run(scenario())


def test_cumulative_partial_protective_fill_applies_only_new_delta_then_closes(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "partial-progress.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        trade_id = db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.0004,
            entry_price=83000.0,
            entry_fee=0.04,
            reason="partial-progress",
            stop_price=82000.0,
            take_profit_price=84000.0,
        )
        db.set_trade_protection(
            trade_id,
            order_list_id=9200,
            list_client_order_id="agt-prot-progress",
            status="EXECUTING",
        )

        exchange = BinanceClient(settings)
        exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 9200,
            "listClientOrderId": "agt-prot-progress",
            "listOrderStatus": "EXECUTING",
            "orders": [{"orderId": 7200, "clientOrderId": "agt-sl-progress"}],
        })

        states = [
            (
                {
                    "symbol": "BTCUSDT",
                    "orderId": 7200,
                    "clientOrderId": "agt-sl-progress",
                    "status": "PARTIALLY_FILLED",
                    "type": "STOP_LOSS",
                    "side": "SELL",
                    "origQty": "0.00040000",
                    "executedQty": "0.00010000",
                    "price": "0.00000000",
                },
                [
                    {
                        "symbol": "BTCUSDT",
                        "orderId": 7200,
                        "price": "81950.00000000",
                        "qty": "0.00010000",
                        "commission": "0.00100000",
                        "commissionAsset": "USDT",
                    }
                ],
            ),
            (
                {
                    "symbol": "BTCUSDT",
                    "orderId": 7200,
                    "clientOrderId": "agt-sl-progress",
                    "status": "PARTIALLY_FILLED",
                    "type": "STOP_LOSS",
                    "side": "SELL",
                    "origQty": "0.00040000",
                    "executedQty": "0.00025000",
                    "price": "0.00000000",
                },
                [
                    {
                        "symbol": "BTCUSDT",
                        "orderId": 7200,
                        "price": "81950.00000000",
                        "qty": "0.00010000",
                        "commission": "0.00100000",
                        "commissionAsset": "USDT",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "orderId": 7200,
                        "price": "81900.00000000",
                        "qty": "0.00015000",
                        "commission": "0.00150000",
                        "commissionAsset": "USDT",
                    },
                ],
            ),
            (
                {
                    "symbol": "BTCUSDT",
                    "orderId": 7200,
                    "clientOrderId": "agt-sl-progress",
                    "status": "FILLED",
                    "type": "STOP_LOSS",
                    "side": "SELL",
                    "origQty": "0.00040000",
                    "executedQty": "0.00040000",
                    "price": "0.00000000",
                },
                [
                    {
                        "symbol": "BTCUSDT",
                        "orderId": 7200,
                        "price": "81950.00000000",
                        "qty": "0.00010000",
                        "commission": "0.00100000",
                        "commissionAsset": "USDT",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "orderId": 7200,
                        "price": "81900.00000000",
                        "qty": "0.00015000",
                        "commission": "0.00150000",
                        "commissionAsset": "USDT",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "orderId": 7200,
                        "price": "81850.00000000",
                        "qty": "0.00015000",
                        "commission": "0.00150000",
                        "commissionAsset": "USDT",
                    },
                ],
            ),
        ]
        cursor = {"i": 0}

        async def get_order(symbol, *, client_order_id=None, order_id=None):
            return states[cursor["i"]][0]

        async def my_trades(symbol, *, order_id=None):
            fills = states[cursor["i"]][1]
            cursor["i"] += 1
            if cursor["i"] == 2:
                exchange.get_order_list.return_value = {
                    "symbol": "BTCUSDT",
                    "orderListId": 9200,
                    "listClientOrderId": "agt-prot-progress",
                    "listOrderStatus": "ALL_DONE",
                    "orders": [{"orderId": 7200, "clientOrderId": "agt-sl-progress"}],
                }
            return fills

        exchange.get_order = AsyncMock(side_effect=get_order)
        exchange.my_trades = AsyncMock(side_effect=my_trades)
        exchange.market_order = AsyncMock(
            side_effect=AssertionError("Cumulative protective fills must not create a market SELL")
        )

        broker = Broker(settings, exchange, db)

        first = await broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0.0, "first partial"),
            81950.0,
        )
        assert first.details["closed"]["executed_quantity"] == 0.0001
        assert abs(float(db.get_open_trade("BTCUSDT", "testnet")["quantity"]) - 0.0003) < 1e-12

        second = await broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0.0, "second partial"),
            81900.0,
        )
        assert abs(second.details["closed"]["executed_quantity"] - 0.00015) < 1e-12
        assert abs(float(db.get_open_trade("BTCUSDT", "testnet")["quantity"]) - 0.00015) < 1e-12

        third = await broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0.0, "final protective fill"),
            81850.0,
        )
        assert abs(third.details["closed"]["executed_quantity"] - 0.00015) < 1e-12
        assert third.details["closed"]["partial"] is False
        assert db.get_open_trade("BTCUSDT", "testnet") is None
        exchange.market_order.assert_not_awaited()
        await exchange.close()

    asyncio.run(scenario())



def test_oco_submit_timeout_reconciles_by_same_client_id(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "oco-submit-timeout.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        trade_id = db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.001,
            entry_price=100.0,
            entry_fee=0.0,
            reason="timeout-test",
            stop_price=95.0,
            take_profit_price=110.0,
        )
        exchange = BinanceClient(settings)
        exchange.place_protective_oco = AsyncMock(
            side_effect=TimeoutError("submit timeout")
        )
        exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 12345,
            "listClientOrderId": "fixed-protection-id",
            "listOrderStatus": "EXECUTING",
            "orders": [],
        })

        broker = Broker(settings, exchange, db)
        broker._new_protection_ids = lambda: (
            "fixed-protection-id",
            "fixed-tp-id",
            "fixed-sl-id",
        )

        result = await broker._place_exchange_protection(
            db.get_open_trade("BTCUSDT", "testnet")
        )

        assert result["protected"] is True
        assert result["status"] == "EXECUTING"
        assert result["list_client_order_id"] == "fixed-protection-id"
        assert result["order_list_id"] == 12345

        trade = db.get_open_trade("BTCUSDT", "testnet")
        assert trade["protective_list_client_order_id"] == "fixed-protection-id"
        assert trade["protective_order_list_id"] == "12345"
        assert trade["protection_status"] == "EXECUTING"
        exchange.place_protective_oco.assert_awaited_once()
        exchange.get_order_list.assert_awaited_once_with(
            "BTCUSDT",
            list_client_order_id="fixed-protection-id",
        )
        await exchange.close()

    asyncio.run(scenario())


def test_oco_submit_and_reconciliation_failure_persists_unknown_and_does_not_retry(tmp_path):
    async def scenario():
        import pytest

        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "oco-submit-unknown.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.001,
            entry_price=100.0,
            entry_fee=0.0,
            reason="timeout-test",
            stop_price=95.0,
            take_profit_price=110.0,
        )
        exchange = BinanceClient(settings)
        exchange.place_protective_oco = AsyncMock(
            side_effect=TimeoutError("submit timeout")
        )
        exchange.get_order_list = AsyncMock(
            side_effect=TimeoutError("reconcile timeout")
        )

        broker = Broker(settings, exchange, db)
        broker._new_protection_ids = lambda: (
            "unknown-protection-id",
            "unknown-tp-id",
            "unknown-sl-id",
        )

        with pytest.raises(RuntimeError, match="outcome is uncertain"):
            await broker._place_exchange_protection(
                db.get_open_trade("BTCUSDT", "testnet")
            )

        trade = db.get_open_trade("BTCUSDT", "testnet")
        assert trade["protective_list_client_order_id"] == "unknown-protection-id"
        assert trade["protective_order_list_id"] is None
        assert trade["protection_status"] == "PROTECTION_UNKNOWN"

        second = await broker._place_exchange_protection(trade)
        assert second["protected"] is True
        assert second["status"] == "PROTECTION_UNKNOWN"
        assert second["list_client_order_id"] == "unknown-protection-id"
        assert exchange.place_protective_oco.await_count == 1
        await exchange.close()

    asyncio.run(scenario())


def test_oco_cancel_timeout_with_fill_reconciles_without_duplicate_market_sell(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "oco-cancel-race.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        trade_id = db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.001,
            entry_price=100.0,
            entry_fee=0.0,
            reason="cancel-race",
            stop_price=95.0,
            take_profit_price=110.0,
        )
        db.set_trade_protection(
            trade_id,
            order_list_id=555,
            list_client_order_id="cancel-race-list",
            status="EXECUTING",
        )

        exchange = BinanceClient(settings)
        exchange.asset_balance_details = AsyncMock(return_value={
            "free": 0.0,
            "locked": 0.001,
            "total": 0.001,
        })
        exchange.cancel_order_list = AsyncMock(
            side_effect=TimeoutError("cancel timeout")
        )
        exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 555,
            "listClientOrderId": "cancel-race-list",
            "listOrderStatus": "ALL_DONE",
            "orders": [
                {
                    "orderId": 556,
                    "clientOrderId": "cancel-race-sl",
                }
            ],
        })
        exchange.get_order = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderId": 556,
            "clientOrderId": "cancel-race-sl",
            "status": "FILLED",
            "type": "STOP_LOSS",
            "side": "SELL",
            "origQty": "0.00100000",
            "executedQty": "0.00100000",
            "price": "0.00000000",
        })
        exchange.my_trades = AsyncMock(return_value=[
            {
                "symbol": "BTCUSDT",
                "orderId": 556,
                "price": "94.50000000",
                "qty": "0.00100000",
                "commission": "0.00000000",
                "commissionAsset": "USDT",
            }
        ])
        exchange.market_order = AsyncMock(
            side_effect=AssertionError(
                "Cancel timeout race must not submit duplicate market SELL"
            )
        )

        broker = Broker(settings, exchange, db)
        result = await broker.maybe_exit(
            StrategySignal(SignalSide.SELL, 1.0, "software exit"),
            99.0,
        )

        assert result.success is True
        assert result.action == "SELL"
        assert result.message == "Exchange stop loss"
        assert db.get_open_trade("BTCUSDT", "testnet") is None
        exchange.market_order.assert_not_awaited()
        await exchange.close()

    asyncio.run(scenario())


def test_oco_cancel_timeout_and_reconciliation_failure_blocks_market_sell(tmp_path):
    async def scenario():
        import pytest

        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "oco-cancel-unknown.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        trade_id = db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.001,
            entry_price=100.0,
            entry_fee=0.0,
            reason="cancel-unknown",
            stop_price=95.0,
            take_profit_price=110.0,
        )
        db.set_trade_protection(
            trade_id,
            order_list_id=600,
            list_client_order_id="cancel-unknown-list",
            status="EXECUTING",
        )

        exchange = BinanceClient(settings)
        exchange.asset_balance_details = AsyncMock(return_value={
            "free": 0.0,
            "locked": 0.001,
            "total": 0.001,
        })
        exchange.cancel_order_list = AsyncMock(
            side_effect=TimeoutError("cancel timeout")
        )
        exchange.get_order_list = AsyncMock(
            side_effect=TimeoutError("status timeout")
        )
        exchange.market_order = AsyncMock(
            side_effect=AssertionError(
                "Unknown cancel outcome must never submit market SELL"
            )
        )

        broker = Broker(settings, exchange, db)
        with pytest.raises(TimeoutError, match="status timeout"):
            await broker.maybe_exit(
                StrategySignal(SignalSide.SELL, 1.0, "software exit"),
                99.0,
            )

        assert db.get_open_trade("BTCUSDT", "testnet") is not None
        exchange.market_order.assert_not_awaited()
        await exchange.close()

    asyncio.run(scenario())



def test_recovered_buy_gets_exchange_protection_before_reconciliation_completes(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "recover-buy-protection.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        db.record_order_intent(
            mode="testnet",
            symbol="BTCUSDT",
            client_order_id="recover-buy-1",
            side="BUY",
            requested_quantity=0.001,
        )
        db.update_order_record(
            client_order_id="recover-buy-1",
            binance_order_id=None,
            executed_quantity=0.0,
            average_fill_price=None,
            status="UNKNOWN",
            commission_quote=0.0,
            commission_details=[],
        )

        exchange = BinanceClient(settings)
        exchange.get_order = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "side": "BUY",
            "clientOrderId": "recover-buy-1",
            "orderId": 333,
            "status": "FILLED",
            "executedQty": "0.00100000",
            "cummulativeQuoteQty": "100.00000000",
        })
        exchange.my_trades = AsyncMock(return_value=[
            {
                "symbol": "BTCUSDT",
                "orderId": 333,
                "price": "100.00000000",
                "qty": "0.00100000",
                "commission": "0.00000000",
                "commissionAsset": "BTC",
            }
        ])
        exchange.normalize_price = AsyncMock(side_effect=lambda symbol, price: price)
        exchange.place_protective_oco = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 444,
            "listClientOrderId": "recover-prot",
            "listOrderStatus": "EXECUTING",
            "orders": [],
        })

        broker = Broker(settings, exchange, db)
        broker._new_protection_ids = lambda: (
            "recover-prot",
            "recover-tp",
            "recover-sl",
        )

        result = await broker.reconcile_unresolved_orders()

        assert len(result) == 1
        assert result[0]["resolved"] is True
        trade = db.get_open_trade("BTCUSDT", "testnet")
        assert trade is not None
        assert trade["protective_list_client_order_id"] == "recover-prot"
        assert trade["protective_order_list_id"] == "444"
        assert trade["protection_status"] == "EXECUTING"
        exchange.place_protective_oco.assert_awaited_once()
        await exchange.close()

    asyncio.run(scenario())



def test_startup_reconciliation_installs_missing_exchange_protection(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "startup-protection.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.001,
            entry_price=100.0,
            entry_fee=0.0,
            reason="startup-test",
            stop_price=95.0,
            take_profit_price=110.0,
        )

        exchange = BinanceClient(settings)
        exchange.place_protective_oco = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 8080,
            "listClientOrderId": "startup-prot",
            "listOrderStatus": "EXECUTING",
            "orders": [],
        })

        broker = Broker(settings, exchange, db)
        broker._new_protection_ids = lambda: (
            "startup-prot",
            "startup-tp",
            "startup-sl",
        )

        result = await broker.reconcile_open_position_protection()

        assert result["startup_status"] == "PROTECTION_INSTALLED"
        assert result["status"] == "EXECUTING"
        trade = db.get_open_trade("BTCUSDT", "testnet")
        assert trade["protective_list_client_order_id"] == "startup-prot"
        assert trade["protective_order_list_id"] == "8080"
        assert trade["protection_status"] == "EXECUTING"
        exchange.place_protective_oco.assert_awaited_once()
        await exchange.close()

    asyncio.run(scenario())


def test_startup_reconciliation_fails_closed_on_unknown_protection_state(tmp_path):
    async def scenario():
        import pytest

        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "startup-unknown.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()
        trade_id = db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.001,
            entry_price=100.0,
            entry_fee=0.0,
            reason="startup-unknown",
            stop_price=95.0,
            take_profit_price=110.0,
        )
        db.set_trade_protection(
            trade_id,
            order_list_id=None,
            list_client_order_id="startup-unknown-prot",
            status="PROTECTION_UNKNOWN",
        )

        exchange = BinanceClient(settings)
        exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 9090,
            "listClientOrderId": "startup-unknown-prot",
            "listOrderStatus": "UNKNOWN",
            "orders": [],
        })

        broker = Broker(settings, exchange, db)
        with pytest.raises(RuntimeError, match="could not be verified safely"):
            await broker.reconcile_open_position_protection()

        assert db.get_open_trade("BTCUSDT", "testnet") is not None
        await exchange.close()

    asyncio.run(scenario())



def test_readiness_checker_is_read_only_and_reports_clean_testnet_state(tmp_path):
    import check_live_readiness as readiness

    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            symbols="BTCUSDT",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            allow_live_trading=False,
            live_protection_validated=False,
            allow_multi_symbol_live=False,
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "readiness.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()

        exchange = AsyncMock()
        exchange.ping.return_value = True
        exchange.account.return_value = {"balances": []}
        exchange.open_orders.return_value = []
        exchange.close.return_value = None

        with patch.object(readiness, "Settings", return_value=settings), patch.object(
            readiness, "BinanceClient", return_value=exchange
        ):
            result = await readiness.run_readiness_check()

        assert result["ok"] is True
        assert result["summary"]["failed"] == 0
        exchange.ping.assert_awaited_once()
        exchange.account.assert_awaited_once()
        exchange.open_orders.assert_awaited_once()
        assert not hasattr(exchange, "market_order") or exchange.market_order.await_count == 0
        await exchange.close()

    asyncio.run(scenario())


def test_readiness_checker_rejects_live_enabled_configuration(tmp_path):
    import check_live_readiness as readiness

    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="live",
            symbols="BTCUSDT",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            allow_live_trading=True,
            live_protection_validated=True,
            allow_multi_symbol_live=True,
            binance_api_key="key",
            binance_api_secret="secret",
            database_path=str(tmp_path / "readiness-live.db"),
        )
        db = TradingDB(settings.database_path)
        db.init()

        exchange = AsyncMock()
        exchange.ping.return_value = True
        exchange.account.return_value = {"balances": []}
        exchange.open_orders.return_value = []
        exchange.close.return_value = None

        with patch.object(readiness, "Settings", return_value=settings), patch.object(
            readiness, "BinanceClient", return_value=exchange
        ):
            result = await readiness.run_readiness_check()

        failed = {item["check"] for item in result["checks"] if not item["ok"]}
        assert "mode_is_testnet" in failed
        assert "live_execution_disabled" in failed
        assert "live_validation_gate_still_closed" in failed
        assert "multi_symbol_live_disabled" in failed
        assert result["ok"] is False
        await exchange.close()

    asyncio.run(scenario())
