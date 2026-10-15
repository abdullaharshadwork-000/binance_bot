"""Signal analytics report: correlate logged strategy decisions with trade outcomes.

Every completed-candle decision is persisted to the signal_log table. This
read-only tool joins logged BUY signals to the trades they produced and
aggregates outcomes, so entry filters can be evaluated against the bot's own
history instead of intuition. It never mutates the database and never places
orders.

Matching heuristic: a closed trade is attributed to the newest BUY signal for
the same symbol/mode logged within --max-signal-age-minutes before the trade
opened (the live engine only enters on fresh completed candles). Trades with
no matching signal are reported as unmatched and excluded from per-feature
aggregates.

This is descriptive statistics over small samples; it does not prove that any
filter causes wins or establish profitability.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime

from app.config import Settings
from app.storage.db import TradingDB


def _bucket(value: float | None, edges: list[float], labels: list[str]) -> str:
    if value is None:
        return "unknown"
    for edge, label in zip(edges, labels, strict=False):
        if value < edge:
            return label
    return labels[-1]


def confidence_bucket(confidence: float | None) -> str:
    if confidence is None:
        return "unknown"
    lo = 0.5 + 0.05 * int(max(0.0, min(confidence, 0.999) - 0.5) / 0.05)
    return f"{lo:.2f}-{lo + 0.05:.2f}"


def _feature(report_aggregates: dict, group: str, key: str, won: bool) -> None:
    bucket = report_aggregates[group]
    item = bucket.setdefault(key, {"trades": 0, "wins": 0})
    item["trades"] += 1
    if won:
        item["wins"] += 1


def _finalize(aggregates: dict) -> dict:
    finalized = {}
    for group, buckets in aggregates.items():
        finalized[group] = {}
        for key, item in buckets.items():
            finalized[group][key] = {
                "trades": item["trades"],
                "wins": item["wins"],
                "win_rate": round(item["wins"] / item["trades"], 4),
            }
    return finalized


def build_signal_report(
    db: TradingDB,
    *,
    mode: str,
    symbol: str,
    max_signal_age_minutes: float = 30.0,
) -> dict:
    """Join closed trades to their logged BUY signals and aggregate outcomes."""
    signals = list(reversed(db.list_signals(mode=mode, symbol=symbol, side="BUY")))
    trades = list(reversed(db.closed_trades(mode=mode, symbol=symbol)))
    max_age_seconds = max_signal_age_minutes * 60

    matched: list[dict] = []
    unmatched_trades = 0
    for trade in trades:
        opened_at = trade.get("opened_at")
        if not opened_at:
            unmatched_trades += 1
            continue
        candidates = [
            s for s in signals
            if s["created_at"] <= opened_at
            and (
                datetime.fromisoformat(opened_at)
                - datetime.fromisoformat(s["created_at"])
            ).total_seconds()
            <= max_age_seconds
        ]
        if not candidates:
            unmatched_trades += 1
            continue
        signal = candidates[-1]
        features = json.loads(signal["features"]) if signal["features"] else {}
        matched.append({
            "trade_id": trade["id"],
            "pnl": float(trade.get("pnl") or 0.0),
            "pnl_pct": float(trade.get("pnl_pct") or 0.0),
            "exit_reason": trade.get("exit_reason") or "unknown",
            "confidence": float(signal["confidence"]),
            "adx": features.get("adx"),
            "atr_pct": features.get("atr_pct"),
            "volume_ratio": features.get("volume_ratio"),
            "age_seconds": (
                datetime.fromisoformat(opened_at)
                - datetime.fromisoformat(signal["created_at"])
            ).total_seconds(),
        })

    overall_aggregates: dict = defaultdict(lambda: {"trades": 0, "wins": 0})
    group_aggregates: dict = {
        "by_confidence": defaultdict(dict),
        "by_adx": defaultdict(dict),
        "by_atr_pct": defaultdict(dict),
        "by_volume_ratio": defaultdict(dict),
        "by_exit_reason": defaultdict(dict),
    }
    wins = 0
    pnl_pcts: list[float] = []
    for item in matched:
        won = item["pnl"] > 0
        wins += int(won)
        pnl_pcts.append(item["pnl_pct"])

        overall_aggregates["all"]["trades"] += 1
        if won:
            overall_aggregates["all"]["wins"] += 1

        _feature(group_aggregates, "by_confidence", confidence_bucket(item["confidence"]), won)
        _feature(
            group_aggregates,
            "by_adx",
            _bucket(item["adx"], [16, 20, 25, 30], ["<16", "16-20", "20-25", "25-30", "30+"]),
            won,
        )
        _feature(
            group_aggregates,
            "by_atr_pct",
            _bucket(
                item["atr_pct"],
                [0.005, 0.01, 0.02, 0.04],
                ["<0.5%", "0.5-1%", "1-2%", "2-4%", "4%+"],
            ),
            won,
        )
        _feature(
            group_aggregates,
            "by_volume_ratio",
            _bucket(item["volume_ratio"], [1.0, 1.5], ["<1.0", "1.0-1.5", ">1.5"]),
            won,
        )
        _feature(group_aggregates, "by_exit_reason", item["exit_reason"], won)

    total_matched = len(matched)
    return {
        "mode": mode,
        "symbol": symbol,
        "generated_at": datetime.now(UTC).isoformat(),
        "logged_buy_signals": len(signals),
        "closed_trades": len(trades),
        "matched_trades": total_matched,
        "unmatched_trades": unmatched_trades,
        "overall": {
            "trades": total_matched,
            "wins": wins,
            "losses": total_matched - wins,
            "win_rate": round(wins / total_matched, 4) if total_matched else None,
            "avg_pnl_pct": (
                round(sum(pnl_pcts) / len(pnl_pcts), 6) if pnl_pcts else None
            ),
        },
        **_finalize(group_aggregates),
        "limitations": [
            "Matching is heuristic: trades are attributed to the newest BUY signal within the age window.",
            "Descriptive statistics on small samples; correlation with outcomes is not causation.",
            "LLM confidence adjustments, manual actions, and adaptive thresholds are not reconstructed here.",
        ],
    }


def main() -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(
        description=(
            "Analyze logged strategy signals against closed-trade outcomes "
            "(read-only; uses the signal_log table)."
        )
    )
    parser.add_argument("--symbol", default=settings.symbol, help="Symbol to analyze (default: primary configured symbol).")
    parser.add_argument("--mode", default=settings.mode, choices=["paper", "testnet", "live"], help="Trading mode to analyze (default: configured MODE).")
    parser.add_argument("--database", default=settings.database_path, help="Path to the bot SQLite database.")
    parser.add_argument(
        "--max-signal-age-minutes",
        type=float,
        default=30.0,
        help="Maximum age of a BUY signal to be credited with a trade (default: 30 minutes).",
    )
    args = parser.parse_args()

    if args.max_signal_age_minutes <= 0:
        parser.error("--max-signal-age-minutes must be positive")

    import sqlite3

    db = TradingDB(args.database)
    try:
        report = build_signal_report(
            db,
            mode=args.mode,
            symbol=args.symbol,
            max_signal_age_minutes=args.max_signal_age_minutes,
        )
    except sqlite3.OperationalError as exc:
        print(
            f"Cannot read the signal log at {args.database}: {exc}\n"
            "Run the bot first so the database is created, or pass --database "
            "pointing at the bot's SQLite file.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
