import asyncio
from unittest.mock import AsyncMock

from app.agents.multi_orchestrator import MultiSymbolTradingManager, PortfolioCoordinator
from app.config import Settings
from app.exchange.binance import BinanceClient
from app.execution.broker import Broker
from app.models import ExecutionResult, SignalSide, StrategySignal


def make_settings(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "mode": "testnet",
        "binance_api_key": "test-key",
        "binance_api_secret": "test-secret",
        "quote_asset": "USDT",
        "symbols": "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT",
        "max_concurrent_positions": 2,
        "database_path": str(tmp_path / "multi.db"),
        "learning_profile_path": str(tmp_path / "learning.json"),
        "enable_adaptive_learning": False,
        "enable_llm_advisor": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_multi_symbol_settings_are_normalized(tmp_path):
    settings = make_settings(
        tmp_path,
        symbols=" btcusdt, ethusdt, BTCUSDT , solusdt ",
    )

    assert settings.trading_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert settings.symbol == "BTCUSDT"
    assert settings.base_asset == "BTC"
    assert settings.base_for_symbol("ETHUSDT") == "ETH"


def test_manager_builds_one_isolated_bot_per_symbol(tmp_path):
    settings = make_settings(tmp_path)
    manager = MultiSymbolTradingManager(settings)

    assert manager.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
    assert manager.get_bot("ETHUSDT").settings.base_asset == "ETH"
    assert manager.get_bot("SOLUSDT").settings.base_asset == "SOL"

    profiles = {
        bot.settings.learning_profile_path
        for bot in manager.bots.values()
    }
    assert len(profiles) == 4


def test_portfolio_position_limit_blocks_additional_entry(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path, symbols="BTCUSDT,ETHUSDT,SOLUSDT")
        portfolio = PortfolioCoordinator(settings)

        portfolio.db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.01,
            entry_price=100,
            entry_fee=0,
            reason="test",
            stop_price=95,
            take_profit_price=110,
        )
        portfolio.db.open_trade(
            mode="testnet",
            symbol="ETHUSDT",
            quantity=0.1,
            entry_price=100,
            entry_fee=0,
            reason="test",
            stop_price=95,
            take_profit_price=110,
        )

        called = False

        async def submit():
            nonlocal called
            called = True
            return ExecutionResult("BUY", True, "should not run")

        result = await portfolio.execute_entry("SOLUSDT", submit)

        assert result.success is False
        assert "Portfolio position limit reached" in result.message
        assert called is False

    asyncio.run(scenario())


def test_portfolio_daily_pnl_combines_configured_symbols(tmp_path):
    settings = make_settings(tmp_path, symbols="BTCUSDT,ETHUSDT")
    portfolio = PortfolioCoordinator(settings)

    first = portfolio.db.open_trade(
        mode="testnet",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="test",
        stop_price=95,
        take_profit_price=110,
    )
    portfolio.db.close_trade(first, 110, 0, "profit")

    second = portfolio.db.open_trade(
        mode="testnet",
        symbol="ETHUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="test",
        stop_price=95,
        take_profit_price=110,
    )
    portfolio.db.close_trade(second, 95, 0, "loss")

    assert portfolio.realized_pnl_today() == 5.0


def test_multi_symbol_live_requires_separate_opt_in(tmp_path):
    try:
        make_settings(
            tmp_path,
            mode="live",
            symbols="BTCUSDT,ETHUSDT",
            allow_live_trading=True,
            allow_multi_symbol_live=False,
        )
    except ValueError as exc:
        assert "Multi-symbol live trading is blocked" in str(exc)
    else:
        raise AssertionError("Expected multi-symbol live mode to be rejected")



def test_unresolved_eth_order_does_not_block_btc_protective_reconciliation(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path, symbols="BTCUSDT,ETHUSDT")
        db = PortfolioCoordinator(settings).db

        btc_trade_id = db.open_trade(
            mode="testnet",
            symbol="BTCUSDT",
            quantity=0.001,
            entry_price=100.0,
            entry_fee=0.0,
            reason="btc-managed",
            stop_price=95.0,
            take_profit_price=110.0,
        )
        db.set_trade_protection(
            btc_trade_id,
            order_list_id=1001,
            list_client_order_id="btc-protection",
            status="EXECUTING",
        )

        # ETH has an uncertain order. This must block new portfolio entries,
        # but it must never prevent BTC from reconciling its own protective exit.
        db.record_order_intent(
            mode="testnet",
            symbol="ETHUSDT",
            client_order_id="eth-uncertain",
            side="BUY",
            requested_quantity=0.1,
        )

        btc_settings = settings.model_copy(
            update={
                "symbol": "BTCUSDT",
                "base_asset": "BTC",
                "symbols": "BTCUSDT",
            }
        )
        exchange = BinanceClient(btc_settings)
        exchange.get_order_list = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderListId": 1001,
            "listClientOrderId": "btc-protection",
            "listOrderStatus": "ALL_DONE",
            "orders": [
                {
                    "orderId": 1002,
                    "clientOrderId": "btc-stop",
                }
            ],
        })
        exchange.get_order = AsyncMock(return_value={
            "symbol": "BTCUSDT",
            "orderId": 1002,
            "clientOrderId": "btc-stop",
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
                "orderId": 1002,
                "price": "94.50000000",
                "qty": "0.00100000",
                "commission": "0.00000000",
                "commissionAsset": "USDT",
            }
        ])
        exchange.market_order = AsyncMock(
            side_effect=AssertionError(
                "BTC protective reconciliation must not submit a duplicate SELL"
            )
        )

        broker = Broker(btc_settings, exchange, db)
        result = await broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0.0, "btc reconcile"),
            94.5,
        )

        assert result.success is True
        assert result.action == "SELL"
        assert result.message == "Exchange stop loss"
        assert db.get_open_trade("BTCUSDT", "testnet") is None
        assert db.has_unresolved_order(mode="testnet", symbol="ETHUSDT") is True
        exchange.market_order.assert_not_awaited()
        await exchange.close()

    asyncio.run(scenario())


def test_unresolved_symbol_blocks_new_portfolio_entry_but_not_other_symbol_state(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path, symbols="BTCUSDT,ETHUSDT,SOLUSDT")
        portfolio = PortfolioCoordinator(settings)

        portfolio.db.record_order_intent(
            mode="testnet",
            symbol="ETHUSDT",
            client_order_id="eth-uncertain-entry",
            side="BUY",
            requested_quantity=0.1,
        )

        class StubBot:
            def market_snapshot(self):
                return {"price": 100.0, "price_age_ms": 0.0}

        portfolio.bots = {
            "BTCUSDT": StubBot(),
            "ETHUSDT": StubBot(),
            "SOLUSDT": StubBot(),
        }
        portfolio._balances = {
            "balances": [
                {"asset": "USDT", "free": "1000", "locked": "0"},
                {"asset": "BTC", "free": "0", "locked": "0"},
                {"asset": "ETH", "free": "0", "locked": "0"},
                {"asset": "SOL", "free": "0", "locked": "0"},
            ]
        }
        portfolio._balance_time = __import__("time").monotonic()

        called = False

        async def submit():
            nonlocal called
            called = True
            return ExecutionResult("BUY", True, "submitted")

        result = await portfolio.execute_entry(
            "SOLUSDT",
            submit,
            notional=10.0,
            expected_price=100.0,
        )

        assert result.success is False
        assert "Unresolved order on ETHUSDT" in result.message
        assert called is False
        assert portfolio.db.get_open_trade("BTCUSDT", "testnet") is None
        assert portfolio.db.get_open_trade("SOLUSDT", "testnet") is None

    asyncio.run(scenario())


def test_analyze_all_isolates_one_symbol_failure(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path, symbols="BTCUSDT,ETHUSDT,SOLUSDT")
        manager = MultiSymbolTradingManager(settings)

        manager.bots["BTCUSDT"].analyze_once = AsyncMock(
            return_value={"symbol": "BTCUSDT", "ok": True}
        )
        manager.bots["ETHUSDT"].analyze_once = AsyncMock(
            side_effect=RuntimeError("ETH exchange unavailable")
        )
        manager.bots["SOLUSDT"].analyze_once = AsyncMock(
            return_value={"symbol": "SOLUSDT", "ok": True}
        )

        result = await manager.analyze_all()

        assert result["BTCUSDT"]["ok"] is True
        assert "ETH exchange unavailable" in result["ETHUSDT"]["error"]
        assert result["SOLUSDT"]["ok"] is True

        for bot in manager.bots.values():
            await bot.exchange.close()

    asyncio.run(scenario())
