import asyncio

from app.agents.multi_orchestrator import MultiSymbolTradingManager, PortfolioCoordinator
from app.config import Settings
from app.models import ExecutionResult


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
