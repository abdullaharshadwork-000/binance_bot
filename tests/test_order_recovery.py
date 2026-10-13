import asyncio
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.exchange.binance import BinanceClient
from app.execution.broker import Broker
from app.models import RiskDecision, SignalSide, StrategySignal
from app.storage.db import TradingDB


def make_broker(tmp_path, mode="testnet"):
    settings = Settings(
        _env_file=None,
        mode=mode,
        allow_live_trading=(mode == "live"),
        live_protection_validated=(mode == "live"),
        binance_api_key="test",
        binance_api_secret="test",
        database_path=str(tmp_path / f"{mode}.db"),
    )
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
        "orders": [
            {
                "symbol": symbol,
                "orderId": 9002,
                "clientOrderId": kwargs["take_profit_client_order_id"],
            },
            {
                "symbol": symbol,
                "orderId": 9003,
                "clientOrderId": kwargs["stop_client_order_id"],
            },
        ],
    })
    async def protective_order_list(symbol, *, list_client_order_id):
        token = list_client_order_id.removeprefix("agt-prot-")
        return {
            "symbol": symbol,
            "orderListId": 9001,
            "listClientOrderId": list_client_order_id,
            "listStatusType": "EXEC_STARTED",
            "listOrderStatus": "EXECUTING",
            "orders": [
                {
                    "symbol": symbol,
                    "orderId": 9002,
                    "clientOrderId": f"agt-tp-{token}",
                },
                {
                    "symbol": symbol,
                    "orderId": 9003,
                    "clientOrderId": f"agt-sl-{token}",
                },
            ],
        }

    async def protective_child_order(symbol, *, client_order_id):
        is_take_profit = client_order_id.startswith("agt-tp-")
        return {
            "symbol": symbol,
            "orderId": 9002 if is_take_profit else 9003,
            "clientOrderId": client_order_id,
            "status": "NEW",
            "type": "LIMIT_MAKER" if is_take_profit else "STOP_LOSS",
            "side": "SELL",
            "executedQty": "0",
            "price": "102" if is_take_profit else "0",
        }

    exchange.get_order_list = AsyncMock(side_effect=protective_order_list)
    exchange.get_order = AsyncMock(side_effect=protective_child_order)
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


def test_exit_database_failure_never_submits_untracked_sell(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        await buy(broker)
        with broker.db.connection() as conn:
            conn.execute(
                "CREATE TRIGGER fail_exit BEFORE UPDATE ON trades "
                "BEGIN SELECT RAISE(ABORT, 'disk failure'); END"
            )

        with pytest.raises(Exception, match="disk failure"):
            await broker.maybe_exit(
                StrategySignal(SignalSide.SELL, .8, "exit"),
                100,
            )

        # The only market order is the original BUY. If local protection state
        # cannot be updated, fail closed instead of submitting an untracked SELL.
        assert broker.exchange.market_order.await_count == 1
        assert broker.db.get_open_trade("BTCUSDT", "testnet") is not None

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


def test_unpriceable_third_asset_fee_uses_estimate_and_keeps_position_protected(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        original = broker.exchange.market_order.side_effect

        async def third_asset_fill(symbol, side, quantity, *, client_order_id):
            order = await original(
                symbol,
                side,
                quantity,
                client_order_id=client_order_id,
            )
            order["fills"][0]["commission"] = "0.001"
            order["fills"][0]["commissionAsset"] = "UNPRICED"
            return order

        broker.exchange.market_order.side_effect = third_asset_fill
        broker.exchange.asset_quote_conversion_pairs = AsyncMock(return_value={})
        broker.exchange.ticker_prices = AsyncMock()

        result = await buy(broker)

        assert result.success
        assert result.details["entry_fee_quote"] == 0.1
        assert result.details["fee_valuation"]["estimated"] is True
        assert result.details["fee_valuation"]["unpriced_assets"] == ["UNPRICED"]
        assert result.details["commissions"][0] == {
            "asset": "UNPRICED",
            "amount": 0.001,
        }
        trade = broker.db.get_open_trade("BTCUSDT", "testnet")
        assert trade is not None
        assert trade["entry_fee"] == 0.1
        assert trade["protection_status"] == "EXECUTING"
        order_record = broker.db.get_order_by_client_id(
            result.details["client_order_id"]
        )
        assert order_record["ledger_applied"] == 1
        assert "QUOTE_FEE_VALUATION" in order_record["commission_details"]
        broker.exchange.market_order.assert_awaited_once()
        broker.exchange.place_protective_oco.assert_awaited_once()

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
        broker.exchange.cancel_order_list.assert_not_awaited()

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


@pytest.mark.parametrize(
    "change",
    [
        "missing_list_id",
        "wrong_list_client_id",
        "inactive_list",
        "missing_status",
        "missing_child",
        "wrong_child_id",
        "wrong_child_symbol",
        "duplicate_order_id",
        "wrong_child_side",
        "wrong_child_type",
    ],
)
def test_unverified_oco_response_persists_unknown_without_reporting_protected(
    tmp_path,
    change,
):
    async def scenario():
        broker = make_broker(tmp_path)
        trade_id = broker.db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=1,
            entry_price=100,
            entry_fee=0,
            reason="test protection validation",
            stop_price=95,
            take_profit_price=110,
        )
        broker._new_protection_ids = lambda: (
            "validation-list",
            "validation-tp",
            "validation-sl",
        )
        response = {
            "symbol": "BTCUSDT",
            "orderListId": 9100,
            "listClientOrderId": "validation-list",
            "listOrderStatus": "EXECUTING",
            "orders": [
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9101,
                    "clientOrderId": "validation-tp",
                    "side": "SELL",
                    "type": "LIMIT_MAKER",
                },
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9102,
                    "clientOrderId": "validation-sl",
                    "side": "SELL",
                    "type": "STOP_LOSS",
                },
            ],
        }
        if change == "missing_list_id":
            response.pop("orderListId")
        elif change == "wrong_list_client_id":
            response["listClientOrderId"] = "unexpected-list"
        elif change == "inactive_list":
            response["listOrderStatus"] = "REJECT"
        elif change == "missing_status":
            response.pop("listOrderStatus")
        elif change == "missing_child":
            response["orders"].pop()
        elif change == "wrong_child_id":
            response["orders"][1]["clientOrderId"] = "unexpected-sl"
        elif change == "wrong_child_symbol":
            response["orders"][1]["symbol"] = "ETHUSDT"
        elif change == "duplicate_order_id":
            response["orders"][1]["orderId"] = 9101
        elif change == "wrong_child_side":
            response["orders"][0]["side"] = "BUY"
        elif change == "wrong_child_type":
            response["orders"][1]["type"] = "LIMIT_MAKER"
        broker.exchange.place_protective_oco = AsyncMock(return_value=response)

        trade = broker.db.get_open_trade("BTCUSDT", "testnet")
        with pytest.raises(RuntimeError, match="could not be verified"):
            await broker._place_exchange_protection(trade)

        persisted = broker.db.get_open_trade("BTCUSDT", "testnet")
        assert persisted["id"] == trade_id
        assert persisted["protective_list_client_order_id"] == "validation-list"
        assert persisted["protection_status"] == "PROTECTION_UNKNOWN"
        second = await broker._place_exchange_protection(persisted)
        assert second["protected"] is False
        assert broker.exchange.place_protective_oco.await_count == 1

    asyncio.run(scenario())


def test_protection_id_is_persisted_before_submitting_oco(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        broker.db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=1,
            entry_price=100,
            entry_fee=0,
            reason="test pre-submit persistence",
            stop_price=95,
            take_profit_price=110,
        )
        broker._new_protection_ids = lambda: (
            "before-submit-list",
            "before-submit-tp",
            "before-submit-sl",
        )

        async def submit(symbol, **kwargs):
            trade = broker.db.get_open_trade("BTCUSDT", "testnet")
            assert trade["protective_list_client_order_id"] == "before-submit-list"
            assert trade["protection_status"] == "PROTECTION_SUBMITTING"
            return {
                "symbol": symbol,
                "orderListId": 9200,
                "listClientOrderId": kwargs["list_client_order_id"],
                "listOrderStatus": "EXECUTING",
                "orders": [
                    {"symbol": symbol, "orderId": 9201, "clientOrderId": kwargs["take_profit_client_order_id"]},
                    {"symbol": symbol, "orderId": 9202, "clientOrderId": kwargs["stop_client_order_id"]},
                ],
            }

        broker.exchange.place_protective_oco = AsyncMock(side_effect=submit)
        trade = broker.db.get_open_trade("BTCUSDT", "testnet")
        result = await broker._place_exchange_protection(trade)

        assert result["protected"] is True
        assert broker.db.get_open_trade("BTCUSDT", "testnet")["protection_status"] == "EXECUTING"
        broker.exchange.place_protective_oco.assert_awaited_once()

    asyncio.run(scenario())


def test_oco_submission_is_blocked_if_protection_intent_cannot_be_persisted(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        broker.db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=1,
            entry_price=100,
            entry_fee=0,
            reason="test protection persistence failure",
            stop_price=95,
            take_profit_price=110,
        )
        with broker.db.connection() as conn:
            conn.execute(
                "CREATE TRIGGER fail_protection_intent BEFORE UPDATE OF "
                "protective_list_client_order_id ON trades "
                "BEGIN SELECT RAISE(ABORT, 'protection persistence failure'); END"
            )

        trade = broker.db.get_open_trade("BTCUSDT", "testnet")
        with pytest.raises(Exception, match="protection persistence failure"):
            await broker._place_exchange_protection(trade)
        broker.exchange.place_protective_oco.assert_not_awaited()
        persisted = broker.db.get_open_trade("BTCUSDT", "testnet")
        assert persisted["protective_list_client_order_id"] is None

    asyncio.run(scenario())


def test_exchange_protective_fill_closes_local_position_after_restart(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        broker._new_protection_ids = lambda: (
            "agt-prot-filled",
            "agt-tp-filled",
            "agt-sl-filled",
        )
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
                {"symbol": "BTCUSDT", "orderId": 7002, "clientOrderId": "agt-sl-filled"},
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
        trade = broker.db.get_open_trade("BTCUSDT", "testnet")
        list_client_id = trade["protective_list_client_order_id"]
        token = list_client_id.removeprefix("agt-prot-")
        broker.exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 9001,
            "listClientOrderId": list_client_id,
            "listStatusType": "ALL_DONE",
            "listOrderStatus": "ALL_DONE",
            "orders": [
                {"symbol": "BTCUSDT", "orderId": 9002, "clientOrderId": f"agt-tp-{token}"},
                {"symbol": "BTCUSDT", "orderId": 9003, "clientOrderId": f"agt-sl-{token}"},
            ],
        })
        broker.exchange.get_order = AsyncMock(side_effect=lambda symbol, *, client_order_id: {
            "symbol": symbol,
            "orderId": 9002 if client_order_id.startswith("agt-tp-") else 9003,
            "clientOrderId": client_order_id,
            "status": "CANCELED",
            "type": "LIMIT_MAKER" if client_order_id.startswith("agt-tp-") else "STOP_LOSS",
            "side": "SELL",
            "executedQty": "0",
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


def test_live_buy_uses_same_exchange_protection_flow(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path, mode="live")
        result = await buy(broker)

        assert result.success is True
        broker.exchange.place_protective_oco.assert_awaited_once()
        trade = broker.db.get_open_trade("BTCUSDT", "live")
        assert trade is not None
        assert trade["protective_list_client_order_id"].startswith("agt-prot-")
        assert trade["protection_status"] == "EXECUTING"

    asyncio.run(scenario())


def test_live_missing_protection_is_restored_while_position_is_inside_hard_exits(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path, mode="live")
        broker.db.open_trade(
            mode="live",
            symbol="BTCUSDT",
            quantity=1,
            entry_price=100,
            entry_fee=0,
            reason="restored live position",
            stop_price=95,
            take_profit_price=110,
        )

        result = await broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0, "hold"),
            100,
        )

        assert result.action == "HOLD"
        assert result.success is True
        broker.exchange.place_protective_oco.assert_awaited_once()
        trade = broker.db.get_open_trade("BTCUSDT", "live")
        assert trade["protective_list_client_order_id"] is not None

    asyncio.run(scenario())


def test_live_unprotected_position_beyond_stop_sells_instead_of_trying_new_oco(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path, mode="live")
        broker.db.open_trade(
            mode="live",
            symbol="BTCUSDT",
            quantity=1,
            entry_price=100,
            entry_fee=0,
            reason="emergency live position",
            stop_price=95,
            take_profit_price=110,
        )

        result = await broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0, "hold"),
            94,
        )

        assert result.action == "SELL"
        assert result.success is True
        broker.exchange.place_protective_oco.assert_not_awaited()
        assert broker.exchange.market_order.await_count == 1
        assert broker.db.get_open_trade("BTCUSDT", "live") is None

    asyncio.run(scenario())


def test_restart_reconciliation_fails_closed_on_incomplete_protective_order_list(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        await buy(broker)
        trade = broker.db.get_open_trade("BTCUSDT", "testnet")
        list_client_id = trade["protective_list_client_order_id"]
        token = list_client_id.removeprefix("agt-prot-")
        broker.exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": int(trade["protective_order_list_id"]),
            "listClientOrderId": list_client_id,
            "listOrderStatus": "EXECUTING",
            "orders": [
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9102,
                    "clientOrderId": f"agt-sl-{token}",
                }
            ],
        })

        with pytest.raises(RuntimeError, match="could not be verified safely"):
            await broker.reconcile_open_position_protection()

        refreshed = broker.db.get_open_trade("BTCUSDT", "testnet")
        assert refreshed is not None
        assert refreshed["protection_status"] == "PROTECTION_UNKNOWN"
        assert refreshed["protective_list_client_order_id"] == list_client_id
        assert broker.exchange.place_protective_oco.await_count == 1
        assert broker.exchange.market_order.await_count == 1

    asyncio.run(scenario())


def test_restart_reconciliation_fails_closed_on_mismatched_protective_child(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        await buy(broker)
        trade = broker.db.get_open_trade("BTCUSDT", "testnet")
        list_client_id = trade["protective_list_client_order_id"]
        token = list_client_id.removeprefix("agt-prot-")
        broker.exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": int(trade["protective_order_list_id"]),
            "listClientOrderId": list_client_id,
            "listOrderStatus": "EXECUTING",
            "orders": [
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9102,
                    "clientOrderId": f"agt-sl-{token}",
                },
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9103,
                    "clientOrderId": f"agt-tp-{token}",
                },
            ],
        })
        broker.exchange.get_order = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderId": 9999,
            "clientOrderId": f"agt-sl-{token}",
            "status": "NEW",
            "type": "STOP_LOSS",
            "side": "SELL",
            "executedQty": "0",
        })

        with pytest.raises(RuntimeError, match="child order could not be verified safely"):
            await broker.reconcile_open_position_protection()

        refreshed = broker.db.get_open_trade("BTCUSDT", "testnet")
        assert refreshed["protection_status"] == "PROTECTION_UNKNOWN"
        assert broker.exchange.market_order.await_count == 1
        assert broker.exchange.place_protective_oco.await_count == 1

    asyncio.run(scenario())


def test_startup_rearms_a_verified_terminal_oco_for_the_remaining_position(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        trade_id = broker.db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=1,
            entry_price=100,
            entry_fee=0,
            reason="terminal-oco-with-no-fill",
            stop_price=95,
            take_profit_price=110,
        )
        broker.db.set_trade_protection(
            trade_id,
            order_list_id=9100,
            list_client_order_id="agt-prot-old-terminal",
            status="EXECUTING",
        )
        broker._new_protection_ids = lambda: (
            "agt-prot-rearmed",
            "agt-tp-rearmed",
            "agt-sl-rearmed",
        )
        broker.exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 9100,
            "listClientOrderId": "agt-prot-old-terminal",
            "listOrderStatus": "ALL_DONE",
            "orders": [
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9101,
                    "clientOrderId": "agt-tp-old-terminal",
                },
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9102,
                    "clientOrderId": "agt-sl-old-terminal",
                },
            ],
        })

        async def canceled_child(symbol, *, client_order_id):
            is_take_profit = client_order_id == "agt-tp-old-terminal"
            return {
                "symbol": symbol,
                "orderId": 9101 if is_take_profit else 9102,
                "clientOrderId": client_order_id,
                "status": "CANCELED",
                "type": "LIMIT_MAKER" if is_take_profit else "STOP_LOSS",
                "side": "SELL",
                "executedQty": "0",
            }

        broker.exchange.get_order = AsyncMock(side_effect=canceled_child)

        result = await broker.reconcile_open_position_protection()

        assert result["startup_status"] == "TERMINAL_OCO_RECONCILED_AND_REPROTECTED"
        assert result["protected"] is True
        trade = broker.db.get_open_trade("BTCUSDT", "testnet")
        assert trade["protective_list_client_order_id"] == "agt-prot-rearmed"
        assert trade["protection_status"] == "EXECUTING"
        broker.exchange.place_protective_oco.assert_awaited_once()
        assert broker.exchange.get_order_list.await_count == 1

    asyncio.run(scenario())


def test_rearmed_oco_preserves_cumulative_accounting_after_a_partial_terminal_fill(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        trade_id = broker.db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.0004,
            entry_price=83000,
            entry_fee=0.04,
            reason="partial-terminal-oco",
            stop_price=82000,
            take_profit_price=84000,
        )
        broker.db.set_trade_protection(
            trade_id,
            order_list_id=9100,
            list_client_order_id="agt-prot-oldpartial",
            status="EXECUTING",
        )
        broker._new_protection_ids = lambda: (
            "agt-prot-newpartial",
            "agt-tp-newpartial",
            "agt-sl-newpartial",
        )
        old_list = {
            "symbol": "BTCUSDT",
            "orderListId": 9100,
            "listClientOrderId": "agt-prot-oldpartial",
            "listOrderStatus": "ALL_DONE",
            "orders": [
                {"symbol": "BTCUSDT", "orderId": 9101, "clientOrderId": "agt-tp-oldpartial"},
                {"symbol": "BTCUSDT", "orderId": 9102, "clientOrderId": "agt-sl-oldpartial"},
            ],
        }
        broker.exchange.get_order_list = AsyncMock(return_value=old_list)

        async def child_order(symbol, *, client_order_id):
            if client_order_id == "agt-sl-oldpartial":
                return {
                    "symbol": symbol,
                    "orderId": 9102,
                    "clientOrderId": client_order_id,
                    "status": "CANCELED",
                    "type": "STOP_LOSS",
                    "side": "SELL",
                    "executedQty": "0.0001",
                    "price": "0",
                }
            if client_order_id == "agt-tp-oldpartial":
                return {
                    "symbol": symbol,
                    "orderId": 9101,
                    "clientOrderId": client_order_id,
                    "status": "CANCELED",
                    "type": "LIMIT_MAKER",
                    "side": "SELL",
                    "executedQty": "0",
                    "price": "84000",
                }
            if client_order_id == "agt-sl-newpartial":
                return {
                    "symbol": symbol,
                    "orderId": 9003,
                    "clientOrderId": client_order_id,
                    "status": "PARTIALLY_FILLED",
                    "type": "STOP_LOSS",
                    "side": "SELL",
                    "executedQty": "0.00015",
                    "price": "0",
                }
            return {
                "symbol": symbol,
                "orderId": 9002,
                "clientOrderId": client_order_id,
                "status": "NEW",
                "type": "LIMIT_MAKER",
                "side": "SELL",
                "executedQty": "0",
                "price": "84000",
            }

        async def account_trades(symbol, *, order_id):
            if str(order_id) == "9102":
                return [{
                    "symbol": symbol,
                    "orderId": 9102,
                    "price": "81950",
                    "qty": "0.0001",
                    "commission": "0.001",
                    "commissionAsset": "USDT",
                }]
            return [{
                "symbol": symbol,
                "orderId": 9003,
                "price": "81900",
                "qty": "0.00015",
                "commission": "0.0015",
                "commissionAsset": "USDT",
            }]

        broker.exchange.get_order = AsyncMock(side_effect=child_order)
        broker.exchange.my_trades = AsyncMock(side_effect=account_trades)

        startup = await broker.reconcile_open_position_protection()
        assert startup["protected"] is True
        remaining = broker.db.get_open_trade("BTCUSDT", "testnet")
        assert remaining["protective_list_client_order_id"] == "agt-prot-newpartial"
        assert abs(float(remaining["quantity"]) - 0.0003) < 1e-12
        assert abs(float(remaining["protective_applied_qty"]) - 0.0001) < 1e-12
        assert abs(float(remaining["protective_list_base_qty"]) - 0.0001) < 1e-12

        broker.exchange.get_order_list.return_value = {
            "symbol": "BTCUSDT",
            "orderListId": int(remaining["protective_order_list_id"]),
            "listClientOrderId": "agt-prot-newpartial",
            "listOrderStatus": "EXECUTING",
            "orders": [
                {"symbol": "BTCUSDT", "orderId": 9002, "clientOrderId": "agt-tp-newpartial"},
                {"symbol": "BTCUSDT", "orderId": 9003, "clientOrderId": "agt-sl-newpartial"},
            ],
        }
        fill = await broker._protective_fill(remaining)
        assert fill is not None
        assert abs(fill["closed"]["executed_quantity"] - 0.00015) < 1e-12
        after_new_fill = broker.db.get_open_trade("BTCUSDT", "testnet")
        assert abs(float(after_new_fill["quantity"]) - 0.00015) < 1e-12
        assert abs(float(after_new_fill["protective_applied_qty"]) - 0.00025) < 1e-12

    asyncio.run(scenario())


def test_terminal_oco_placement_is_reconciled_immediately_when_a_leg_fills(tmp_path):
    async def scenario():
        broker = make_broker(tmp_path)
        broker.db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=1,
            entry_price=100,
            entry_fee=0,
            reason="immediate-terminal-oco",
            stop_price=95,
            take_profit_price=102,
        )
        broker._new_protection_ids = lambda: (
            "agt-prot-immediate",
            "agt-tp-immediate",
            "agt-sl-immediate",
        )
        terminal_list = {
            "symbol": "BTCUSDT",
            "orderListId": 9200,
            "listClientOrderId": "agt-prot-immediate",
            "listOrderStatus": "ALL_DONE",
            "orders": [
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9201,
                    "clientOrderId": "agt-tp-immediate",
                },
                {
                    "symbol": "BTCUSDT",
                    "orderId": 9202,
                    "clientOrderId": "agt-sl-immediate",
                },
            ],
        }
        broker.exchange.place_protective_oco = AsyncMock(return_value=terminal_list)
        broker.exchange.get_order_list = AsyncMock(return_value=terminal_list)

        async def order_lookup(symbol, *, client_order_id):
            is_take_profit = client_order_id == "agt-tp-immediate"
            return {
                "symbol": symbol,
                "orderId": 9201 if is_take_profit else 9202,
                "clientOrderId": client_order_id,
                "status": "FILLED" if is_take_profit else "CANCELED",
                "type": "LIMIT_MAKER" if is_take_profit else "STOP_LOSS",
                "side": "SELL",
                "price": "102" if is_take_profit else "0",
                "executedQty": "1" if is_take_profit else "0",
            }

        broker.exchange.get_order = AsyncMock(side_effect=order_lookup)
        broker.exchange.my_trades = AsyncMock(return_value=[{
            "symbol": "BTCUSDT",
            "orderId": 9201,
            "price": "102",
            "qty": "1",
            "commission": "0",
            "commissionAsset": "USDT",
        }])

        result = await broker._place_exchange_protection(
            broker.db.get_open_trade("BTCUSDT", "testnet")
        )

        assert result["protected"] is False
        assert result["status"] == "ALL_DONE"
        assert result["reconciled_fill"]["applied"] is True
        assert broker.db.get_open_trade("BTCUSDT", "testnet") is None
        broker.exchange.get_order_list.assert_awaited_once_with(
            "BTCUSDT",
            list_client_order_id="agt-prot-immediate",
        )

    asyncio.run(scenario())
