from app.storage.db import TradingDB
from app.ui.dashboard import DASHBOARD_HTML


def test_dashboard_has_plain_language_sections():
    assert "Current Decision" in DASHBOARD_HTML
    assert "Risk Engine" in DASHBOARD_HTML
    assert "Recent Trades" in DASHBOARD_HTML
    assert "What the Dashboard Means" in DASHBOARD_HTML


def test_performance_summary_is_scoped_by_mode_and_symbol(tmp_path):
    db = TradingDB(str(tmp_path / "bot.db"))
    db.init()

    first = db.open_trade(
        mode="paper",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="test",
        stop_price=95,
        take_profit_price=110,
    )
    db.close_trade(first, 110, 0, "take profit")

    second = db.open_trade(
        mode="paper",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="test",
        stop_price=95,
        take_profit_price=110,
    )
    db.close_trade(second, 95, 0, "stop loss")

    live = db.open_trade(
        mode="live",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100,
        entry_fee=0,
        reason="different mode",
        stop_price=95,
        take_profit_price=110,
    )

    summary = db.performance_summary(mode="paper", symbol="BTCUSDT")
    assert summary["closed_trades"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["win_rate"] == 0.5
    assert summary["total_pnl"] == 5
    assert len(summary["equity_curve"]) == 2
    assert db.get_open_trade("BTCUSDT", "paper") is None
    assert db.get_open_trade("BTCUSDT", "live")["id"] == live


def test_partial_close_keeps_remaining_position(tmp_path):
    db = TradingDB(str(tmp_path / "bot.db"))
    db.init()
    trade_id = db.open_trade(
        mode="paper",
        symbol="BTCUSDT",
        quantity=2,
        entry_price=100,
        entry_fee=2,
        reason="test",
        stop_price=95,
        take_profit_price=110,
    )

    closed = db.close_trade(
        trade_id,
        110,
        0.5,
        "partial",
        executed_quantity=0.5,
    )

    assert closed["partial"] is True
    assert closed["remaining_quantity"] == 1.5
    open_trade = db.get_open_trade("BTCUSDT", "paper")
    assert open_trade is not None
    assert open_trade["quantity"] == 1.5


def test_fast_market_monitor_defaults():
    from app.config import Settings

    settings = Settings(_env_file=None)
    assert settings.cycle_seconds == 1.0
    assert settings.use_websocket_market_data is True
    assert settings.market_data_stale_seconds >= settings.cycle_seconds


def test_interval_refresh_math():
    from app.agents.orchestrator import interval_to_ms

    assert interval_to_ms("1m") == 60_000
    assert interval_to_ms("15m") == 900_000
    assert interval_to_ms("1h") == 3_600_000
