from app.storage.db import TradingDB
from app.ui.dashboard import DASHBOARD_HTML


def test_dashboard_has_plain_language_sections():
    assert "Current Decision" in DASHBOARD_HTML
    assert "Risk Engine" in DASHBOARD_HTML
    assert "Recent Trades" in DASHBOARD_HTML
    assert "What the Dashboard Means" in DASHBOARD_HTML


def test_performance_summary(tmp_path):
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

    summary = db.performance_summary()
    assert summary["closed_trades"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["win_rate"] == 0.5
    assert summary["total_pnl"] == 5
    assert len(summary["equity_curve"]) == 2


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
