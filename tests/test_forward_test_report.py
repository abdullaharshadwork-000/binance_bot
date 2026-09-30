from pathlib import Path

from app.config import Settings
from app.storage.db import TradingDB
from forward_test_report import build_report


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        mode="testnet",
        symbol="BTCUSDT",
        symbols="BTCUSDT,ETHUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        binance_api_key="key",
        binance_api_secret="secret",
        database_path=str(tmp_path / "report.db"),
        min_trades_for_learning=10,
        trading_fee_bps=10,
    )


def _insert_closed_trade(
    db: TradingDB,
    *,
    symbol: str,
    quantity: float,
    entry_price: float,
    exit_price: float,
    pnl: float,
    pnl_pct: float,
    entry_fee: float = 0.0,
    exit_fee: float = 0.0,
    reason: str = "Exit signal",
) -> None:
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO trades(
                mode, symbol, quantity, entry_price, exit_price,
                entry_fee, exit_fee, pnl, pnl_pct, status,
                entry_reason, exit_reason, stop_price, take_profit_price,
                highest_price, opened_at, closed_at
            ) VALUES (
                'testnet', ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED',
                'entry', ?, ?, ?, ?, '2026-09-29T00:00:00+00:00',
                '2026-09-29T01:00:00+00:00'
            )
            """,
            (
                symbol,
                quantity,
                entry_price,
                exit_price,
                entry_fee,
                exit_fee,
                pnl,
                pnl_pct,
                reason,
                entry_price * 0.99,
                entry_price * 1.02,
                max(entry_price, exit_price),
            ),
        )


def test_forward_report_applies_fee_shortfall_and_stress_slippage(tmp_path):
    settings = _settings(tmp_path)
    db = TradingDB(settings.database_path)
    db.init()

    _insert_closed_trade(
        db,
        symbol="BTCUSDT",
        quantity=1.0,
        entry_price=100.0,
        exit_price=110.0,
        pnl=10.0,
        pnl_pct=0.10,
    )

    report = build_report(
        settings,
        db,
        limit=None,
        fee_bps=10.0,
        slippage_bps=2.0,
    )

    row = report["trades"][0]
    # Round-trip notional = 210. Fee model = 0.21, slippage stress = 0.042.
    assert abs(row["modeled_fees"] - 0.21) < 1e-9
    assert abs(row["fee_adjusted_pnl"] - 9.79) < 1e-9
    assert abs(row["stress_adjusted_pnl"] - 9.748) < 1e-9
    assert abs(report["stress_adjusted"]["total_pnl"] - 9.748) < 1e-9


def test_forward_report_does_not_double_charge_recorded_fees(tmp_path):
    settings = _settings(tmp_path)
    db = TradingDB(settings.database_path)
    db.init()

    _insert_closed_trade(
        db,
        symbol="ETHUSDT",
        quantity=1.0,
        entry_price=100.0,
        exit_price=90.0,
        pnl=-12.0,
        pnl_pct=-0.12,
        entry_fee=0.15,
        exit_fee=0.10,
        reason="Exchange stop loss",
    )

    report = build_report(
        settings,
        db,
        limit=None,
        fee_bps=10.0,
        slippage_bps=0.0,
    )

    row = report["trades"][0]
    # Modeled 10 bps round-trip fee is 0.19, already below recorded 0.25.
    assert abs(row["recorded_fees"] - 0.25) < 1e-9
    assert abs(row["modeled_fees"] - 0.19) < 1e-9
    assert abs(row["fee_adjusted_pnl"] - (-12.0)) < 1e-9


def test_forward_report_tracks_learning_progress_and_per_symbol(tmp_path):
    settings = _settings(tmp_path)
    db = TradingDB(settings.database_path)
    db.init()

    _insert_closed_trade(
        db,
        symbol="BTCUSDT",
        quantity=1.0,
        entry_price=100.0,
        exit_price=105.0,
        pnl=5.0,
        pnl_pct=0.05,
    )
    _insert_closed_trade(
        db,
        symbol="ETHUSDT",
        quantity=1.0,
        entry_price=100.0,
        exit_price=95.0,
        pnl=-5.0,
        pnl_pct=-0.05,
        reason="Exchange stop loss",
    )

    report = build_report(
        settings,
        db,
        limit=None,
        fee_bps=0.0,
        slippage_bps=0.0,
    )

    assert report["closed_trades"] == 2
    assert report["learning_threshold_per_symbol"] == 10
    assert report["learning_progress"]["BTCUSDT"] == {
        "closed_trades": 1,
        "threshold": 10,
        "trades_until_learning": 9,
        "eligible": False,
    }
    assert report["learning_progress"]["ETHUSDT"] == {
        "closed_trades": 1,
        "threshold": 10,
        "trades_until_learning": 9,
        "eligible": False,
    }
    assert report["raw"]["wins"] == 1
    assert report["raw"]["losses"] == 1
    assert report["per_symbol"]["BTCUSDT"]["raw"]["total_pnl"] == 5.0
    assert report["per_symbol"]["ETHUSDT"]["raw"]["total_pnl"] == -5.0
    assert report["exit_reasons"]["Exchange stop loss"] == 1


def test_learning_progress_uses_all_symbol_history_even_when_report_is_limited(tmp_path):
    settings = _settings(tmp_path)
    db = TradingDB(settings.database_path)
    db.init()

    for _ in range(3):
        _insert_closed_trade(
            db,
            symbol="BTCUSDT",
            quantity=1.0,
            entry_price=100.0,
            exit_price=101.0,
            pnl=1.0,
            pnl_pct=0.01,
        )
    _insert_closed_trade(
        db,
        symbol="ETHUSDT",
        quantity=1.0,
        entry_price=100.0,
        exit_price=99.0,
        pnl=-1.0,
        pnl_pct=-0.01,
    )

    report = build_report(
        settings,
        db,
        limit=1,
        fee_bps=0.0,
        slippage_bps=0.0,
    )

    assert report["closed_trades"] == 1
    assert report["learning_progress"]["BTCUSDT"]["closed_trades"] == 3
    assert report["learning_progress"]["BTCUSDT"]["trades_until_learning"] == 7
    assert report["learning_progress"]["ETHUSDT"]["closed_trades"] == 1
