import asyncio
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.exchange.binance import BinanceClient
from app.execution.broker import Broker
from app.models import RiskDecision, SignalSide, StrategySignal
from app.storage.db import TradingDB


def make_broker(tmp_path):
    settings = Settings(_env_file=None, mode="testnet", binance_api_key="test",
                        binance_api_secret="test", database_path=str(tmp_path / "db"))
    db = TradingDB(settings.database_path)
    db.init()
    exchange = BinanceClient(settings)
    exchange.normalize_quantity = AsyncMock(side_effect=lambda s, q, p: q)
    exchange.normalize_price = AsyncMock(side_effect=lambda s, p: p)
    exchange.asset_balance = AsyncMock(return_value=1)
    exchange.asset_balance_details = AsyncMock(
        return_value={"free": 1.0, "locked": 0.0, "total": 1.0}
    )
    exchange.my_trades = AsyncMock(return_value=[])
    exchange.place_protective_oco = AsyncMock(side_effect=lambda symbol, **kwargs: {
        "symbol": symbol,
        "orderListId": 9001,
        "listClientOrderId": kwargs["list_client_order_id"],
        "listStatusType": "EXEC_STARTED",
        "listOrderStatus": "EXECUTING",
        "orders": [],
    })
    exchange.get_order_list = AsyncMock(return_value={
        "symbol": "BTCUSDT",
        "orderListId": 9001,
        "listClientOrderId": "placeholder",
        "listStatusType": "EXEC_STARTED",
        "listOrderStatus": "EXECUTING",
        "orders": [],
    })
    exchange.cancel_order_list = AsyncMock(return_value={
        "symbol": "BTCUSDT",
        "orderListId": 9001,
        "listStatusType": "ALL_DONE",
        "listOrderStatus": "ALL_DONE",
        "orders": [],
    })
    exchange.check_entry_liquidity = AsyncMock(return_value={})
    async def fill(symbol, side, quantity, *, client_order_id):
        return {"symbol": symbol, "side": side, "clientOrderId": client_order_id,
                "orderId": 123, "status": "FILLED", "executedQty": str(quantity),
                "cummulativeQuoteQty": str(quantity * 100),
                "fills": [{"price": "100", "qty": str(quantity), "commission": "0",
                           "commissionAsset": "USDT"}]}
    exchange.market_order = AsyncMock(side_effect=fill)
    return Broker(settings, exchange, db)


async def buy(broker):
    return await broker.enter(StrategySignal(SignalSide.BUY, .9, "test"),
                              RiskDecision(True, "test", quantity=1), 100)


def test_successful_fill_and_position_are_applied_once(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        result = await buy(broker)
        assert result.success
        order_id = result.details["client_order_id"]
        record = broker.db.get_order_by_client_id(order_id)
        assert record["ledger_applied"] == 1
        assert not broker.db.has_unresolved_order(mode="testnet", symbol="BTCUSDT")
        assert not (await buy(broker)).success
        broker.exchange.market_order.assert_awaited_once()
        with pytest.raises(ValueError, match="already recorded"):
            broker.db.open_trade(mode="testnet", symbol="BTCUSDT", quantity=1,
                entry_price=100, entry_fee=0, reason="duplicate", stop_price=99,
                take_profit_price=102, client_order_id=order_id)
        result = await broker.maybe_exit(StrategySignal(SignalSide.SELL, .8, "exit"), 100)
        assert result.success
        assert broker.db.get_open_trade("BTCUSDT", "testnet") is None
        assert broker.db.get_order_by_client_id(result.details["client_order_id"])["ledger_applied"] == 1
    asyncio.run(scenario())


def test_crash_after_fill_stays_blocked_across_repeated_reconciliation(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        broker._risk_prices_from_fill = AsyncMock(side_effect=RuntimeError("crash after fill"))
        with pytest.raises(RuntimeError, match="crash after fill"):
            await buy(broker)
        record = broker.db.unresolved_orders(mode="testnet", symbol="BTCUSDT")[0]
        assert record["status"] == "FILLED" and record["ledger_applied"] == 0
        for _ in range(3):
            restarted = make_broker(tmp_path)
            restarted.exchange.get_order = AsyncMock(return_value={
                "symbol": "BTCUSDT", "side": "BUY", "clientOrderId": record["client_order_id"],
                "status": "FILLED", "executedQty": "1", "cummulativeQuoteQty": "100"})
            result = await restarted.reconcile_unresolved_orders()
            assert not result[0]["resolved"]
            assert result[0]["status"] == "RECOVERY_REQUIRED"
            assert not (await buy(restarted)).success
            restarted.exchange.market_order.assert_not_awaited()
    asyncio.run(scenario())


def test_database_failure_rolls_back_order_application_with_position(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        with broker.db.connection() as conn:
            conn.execute("CREATE TRIGGER fail_position BEFORE INSERT ON trades BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with pytest.raises(Exception, match="disk failure"):
            await buy(broker)
        record = broker.db.unresolved_orders(mode="testnet", symbol="BTCUSDT")[0]
        assert record["ledger_applied"] == 0
        assert broker.db.get_open_trade("BTCUSDT", "testnet") is None
        assert not (await buy(broker)).success
        broker.exchange.market_order.assert_awaited_once()
    asyncio.run(scenario())


def test_exit_database_failure_blocks_duplicate_sell(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        await buy(broker)
        with broker.db.connection() as conn:
            conn.execute("CREATE TRIGGER fail_exit BEFORE UPDATE ON trades BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with pytest.raises(Exception, match="disk failure"):
            await broker.maybe_exit(StrategySignal(SignalSide.SELL, .8, "exit"), 100)
        record = broker.db.unresolved_orders(mode="testnet", symbol="BTCUSDT")[0]
        assert record["side"] == "SELL" and record["ledger_applied"] == 0
        assert not (await broker.maybe_exit(StrategySignal(SignalSide.SELL, .8, "exit"), 100)).success
        assert broker.exchange.market_order.await_count == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["timeout", "missing_fills", "partial", "wrong_symbol", "wrong_side", "wrong_id", "oversized"])
def test_uncertain_or_invalid_response_never_causes_second_submission(tmp_path, change):
    async def scenario():
        broker = make_broker(tmp_path)
        original = broker.exchange.market_order.side_effect
        async def response(*args, **kwargs):
            if change == "timeout":
                raise TimeoutError("unknown outcome")
            order = await original(*args, **kwargs)
            if change == "missing_fills": order.pop("fills")
            if change == "partial": order["status"] = "PARTIALLY_FILLED"
            if change == "wrong_symbol": order["symbol"] = "ETHUSDT"
            if change == "wrong_side": order["side"] = "SELL"
            if change == "wrong_id": order["clientOrderId"] = "other"
            if change == "oversized": order["executedQty"] = "2"
            return order
        broker.exchange.market_order.side_effect = response
        broker.exchange.get_order = AsyncMock(side_effect=TimeoutError("offline"))
        with pytest.raises(RuntimeError): await buy(broker)
        assert not (await buy(broker)).success
        assert broker.db.has_unresolved_order(mode="testnet", symbol="BTCUSDT")
        broker.exchange.market_order.assert_awaited_once()
    asyncio.run(scenario())


def test_cancellation_during_submit_keeps_persistent_intent(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        started = asyncio.Event()
        async def pending(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        broker.exchange.market_order.side_effect = pending
        task = asyncio.create_task(buy(broker))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert broker.db.has_unresolved_order(mode="testnet", symbol="BTCUSDT")
        assert not (await buy(broker)).success
    asyncio.run(scenario())


def test_two_brokers_cannot_submit_duplicate_symbol_entry(tmp_path):
    async def scenario():
        first, second = make_broker(tmp_path), make_broker(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        original = first.exchange.market_order.side_effect
        async def slow(*args, **kwargs):
            started.set()
            await release.wait()
            return await original(*args, **kwargs)
        first.exchange.market_order.side_effect = slow
        task = asyncio.create_task(buy(first))
        await started.wait()
        assert not (await buy(second)).success
        second.exchange.market_order.assert_not_awaited()
        release.set()
        assert (await task).success
    asyncio.run(scenario())


@pytest.mark.parametrize("price", [float("nan"), float("inf"), 0, -1])
def test_invalid_prices_never_reach_order_submission(tmp_path, price):
    async def scenario():
        broker = make_broker(tmp_path)
        assert not (await broker.enter(StrategySignal(SignalSide.BUY, .9, "test"),
            RiskDecision(True, "test", quantity=1), price)).success
        assert not (await broker.maybe_exit(StrategySignal(SignalSide.SELL, .9, "test"), price)).success
        broker.exchange.market_order.assert_not_awaited()
    asyncio.run(scenario())


def test_liquidity_rejection_does_not_submit_or_create_order_intent(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        broker.exchange.check_entry_liquidity.side_effect = ValueError("Entry spread exceeds limit")
        result = await buy(broker)
        assert not result.success
        assert result.details["order_submitted"] is False
        broker.exchange.market_order.assert_not_awaited()
        assert not broker.db.has_unresolved_order(mode="testnet", symbol="BTCUSDT")
    asyncio.run(scenario())


def test_verified_terminal_buy_is_recovered_after_restart(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        broker._risk_prices_from_fill = AsyncMock(side_effect=RuntimeError("crash after fill"))
        with pytest.raises(RuntimeError, match="crash after fill"):
            await buy(broker)

        record = broker.db.unresolved_orders(mode="testnet", symbol="BTCUSDT")[0]
        restarted = make_broker(tmp_path)
        restarted.exchange.get_order = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "side": "BUY",
            "clientOrderId": record["client_order_id"],
            "orderId": 123,
            "status": "FILLED",
            "executedQty": "1",
            "cummulativeQuoteQty": "100",
        })
        restarted.exchange.my_trades = AsyncMock(return_value=[{
            "orderId": 123,
            "price": "100",
            "qty": "1",
            "commission": "0",
            "commissionAsset": "USDT",
        }])

        result = await restarted.reconcile_unresolved_orders()

        assert result[0]["resolved"] is True
        trade = restarted.db.get_open_trade("BTCUSDT", "testnet")
        assert trade is not None
        assert trade["quantity"] == pytest.approx(1)
        assert not restarted.db.has_unresolved_order(
            mode="testnet", symbol="BTCUSDT"
        )

    asyncio.run(scenario())


def test_position_drift_blocks_exit_instead_of_silently_closing_wrong_quantity(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        await buy(broker)
        broker.exchange.asset_balance_details = AsyncMock(
            return_value={"free": 0.4, "locked": 0.0, "total": 0.4}
        )

        result = await broker.maybe_exit(
            StrategySignal(SignalSide.SELL, .8, "exit"),
            100,
        )

        assert result.success is False
        assert "reconciliation required" in result.message.lower()
        assert broker.db.get_open_trade("BTCUSDT", "testnet") is not None
        assert broker.exchange.market_order.await_count == 1

    asyncio.run(scenario())


def test_testnet_buy_installs_exchange_resident_oco(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        result = await buy(broker)

        assert result.success is True
        broker.exchange.place_protective_oco.assert_awaited_once()
        trade = broker.db.get_open_trade("BTCUSDT", "testnet")
        assert trade["protective_order_list_id"] == "9001"
        assert trade["protective_list_client_order_id"].startswith("agt-prot-")
        assert trade["protection_status"] == "EXECUTING"

    asyncio.run(scenario())


def test_exchange_protective_fill_closes_local_position_after_restart(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        await buy(broker)
        trade = broker.db.get_open_trade("BTCUSDT", "testnet")
        list_client_id = trade["protective_list_client_order_id"]

        broker.exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 9001,
            "listClientOrderId": list_client_id,
            "listStatusType": "ALL_DONE",
            "listOrderStatus": "ALL_DONE",
            "orders": [
                {"symbol": "BTCUSDT", "orderId": 7001, "clientOrderId": "agt-tp-filled"},
                {"symbol": "BTCUSDT", "orderId": 7002, "clientOrderId": "agt-sl-canceled"},
            ],
        })

        async def order_lookup(symbol, *, client_order_id):
            if client_order_id == "agt-tp-filled":
                return {
                    "symbol": symbol,
                    "clientOrderId": client_order_id,
                    "orderId": 7001,
                    "status": "FILLED",
                    "type": "LIMIT_MAKER",
                    "side": "SELL",
                    "price": "102",
                    "executedQty": "1",
                    "cummulativeQuoteQty": "102",
                }
            return {
                "symbol": symbol,
                "clientOrderId": client_order_id,
                "orderId": 7002,
                "status": "CANCELED",
                "type": "STOP_LOSS",
                "side": "SELL",
                "price": "0",
                "executedQty": "0",
                "cummulativeQuoteQty": "0",
            }

        broker.exchange.get_order = AsyncMock(side_effect=order_lookup)
        broker.exchange.my_trades = AsyncMock(return_value=[{
            "orderId": 7001,
            "price": "102",
            "qty": "1",
            "commission": "0",
            "commissionAsset": "USDT",
        }])

        result = await broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0, "hold"),
            101,
        )

        assert result.success is True
        assert result.action == "SELL"
        assert broker.db.get_open_trade("BTCUSDT", "testnet") is None
        assert "exchange take profit" in result.message.lower()

    asyncio.run(scenario())


def test_software_exit_cancels_oco_before_market_sell(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        await buy(broker)
        broker.exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 9001,
            "listStatusType": "ALL_DONE",
            "listOrderStatus": "ALL_DONE",
            "orders": [],
        })

        result = await broker.maybe_exit(
            StrategySignal(SignalSide.SELL, .8, "strategy exit"),
            100,
        )

        assert result.success is True
        broker.exchange.cancel_order_list.assert_awaited_once()
        assert broker.exchange.market_order.await_count == 2
        assert broker.db.get_open_trade("BTCUSDT", "testnet") is None

    asyncio.run(scenario())


def test_restart_recreates_missing_protection_for_open_testnet_trade(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        broker.db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=1,
            entry_price=100,
            entry_fee=0,
            reason="legacy open position",
            stop_price=98,
            take_profit_price=104,
        )

        result = await broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0, "hold"),
            100,
        )

        assert result.action == "HOLD"
        broker.exchange.place_protective_oco.assert_awaited_once()
        trade = broker.db.get_open_trade("BTCUSDT", "testnet")
        assert trade["protective_list_client_order_id"] is not None

    asyncio.run(scenario())
