"""Tests for the signal analytics report (signal_log joined to trade outcomes)."""

import pytest

from app.storage.db import TradingDB
from signal_report import build_signal_report, confidence_bucket


@pytest.fixture()
def db(tmp_path):
    database = TradingDB(str(tmp_path / "signals.db"))
    database.init()
    return database


def _record_buy(db, *, confidence, adx=None, atr_pct=None, volume_ratio=None):
    db.record_signal(
        mode="paper",
        symbol="BTCUSDT",
        interval="15m",
        side="BUY",
        confidence=confidence,
        reason="test",
        price=100.0,
        features={"adx": adx, "atr_pct": atr_pct, "volume_ratio": volume_ratio},
        entry_checks={"ADX confirms a trending market": True},
        score_components={"Base qualifying setup": 0.55},
    )


def _open_and_close(db, *, exit_price, reason):
    trade_id = db.open_trade(
        mode="paper",
        symbol="BTCUSDT",
        quantity=1,
        entry_price=100.0,
        entry_fee=0.0,
        reason="test entry",
        stop_price=95.0,
        take_profit_price=110.0,
    )
    return db.close_trade(trade_id, exit_price, 0.0, reason)


def test_confidence_bucket_boundaries():
    assert confidence_bucket(0.72) == "0.70-0.75"
    assert confidence_bucket(0.90) == "0.90-0.95"
    assert confidence_bucket(0.5) == "0.50-0.55"
    assert confidence_bucket(None) == "unknown"


def test_report_matches_trades_to_signals_and_aggregates(db):
    _record_buy(db, confidence=0.9, adx=27.0, atr_pct=0.012, volume_ratio=1.2)
    _open_and_close(db, exit_price=105.0, reason="Take profit")

    _record_buy(db, confidence=0.72, adx=14.0, atr_pct=0.003, volume_ratio=0.8)
    _open_and_close(db, exit_price=97.0, reason="Stop loss")

    report = build_signal_report(db, mode="paper", symbol="BTCUSDT")

    assert report["logged_buy_signals"] == 2
    assert report["closed_trades"] == 2
    assert report["matched_trades"] == 2
    assert report["unmatched_trades"] == 0
    assert report["overall"]["wins"] == 1
    assert report["overall"]["losses"] == 1
    assert report["overall"]["win_rate"] == pytest.approx(0.5)

    assert report["by_confidence"]["0.90-0.95"]["trades"] == 1
    assert report["by_confidence"]["0.90-0.95"]["win_rate"] == 1.0
    assert report["by_confidence"]["0.70-0.75"]["win_rate"] == 0.0

    assert report["by_adx"]["25-30"]["win_rate"] == 1.0
    assert report["by_adx"]["<16"]["win_rate"] == 0.0

    assert report["by_atr_pct"]["1-2%"]["win_rate"] == 1.0
    assert report["by_atr_pct"]["<0.5%"]["win_rate"] == 0.0

    assert report["by_volume_ratio"]["1.0-1.5"]["wins"] == 1
    assert report["by_volume_ratio"]["<1.0"]["wins"] == 0

    assert report["by_exit_reason"]["Take profit"]["trades"] == 1
    assert report["by_exit_reason"]["Stop loss"]["win_rate"] == 0.0


def test_stale_signals_do_not_match_later_trades(db):
    _record_buy(db, confidence=0.85, adx=20.0, atr_pct=0.01, volume_ratio=1.1)
    # Age the signal beyond the matching window.
    with db.connection() as conn:
        conn.execute(
            """
            UPDATE signal_log SET created_at =
                datetime('now', '-2 hours') || '+00:00'
            """
        )
    _open_and_close(db, exit_price=101.0, reason="Strategy exit")

    report = build_signal_report(
        db, mode="paper", symbol="BTCUSDT", max_signal_age_minutes=30.0
    )
    assert report["matched_trades"] == 0
    assert report["unmatched_trades"] == 1
    assert report["overall"]["win_rate"] is None


def test_empty_database_report_is_well_formed(db):
    report = build_signal_report(db, mode="paper", symbol="BTCUSDT")
    assert report["logged_buy_signals"] == 0
    assert report["closed_trades"] == 0
    assert report["overall"]["trades"] == 0
    assert report["overall"]["win_rate"] is None
    assert report["limitations"]
