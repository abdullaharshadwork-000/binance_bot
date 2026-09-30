from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.storage.db import TradingDB


def _notional(trade: dict[str, Any], side: str) -> float:
    quantity = float(trade.get("quantity") or 0.0)
    if side == "entry":
        price = float(trade.get("entry_price") or 0.0)
    else:
        price = float(trade.get("exit_price") or 0.0)
    return max(0.0, quantity * price)


def _scenario_cost(
    trade: dict[str, Any],
    *,
    fee_bps: float,
    slippage_bps: float,
) -> dict[str, float]:
    entry_notional = _notional(trade, "entry")
    exit_notional = _notional(trade, "exit")
    round_trip_notional = entry_notional + exit_notional

    recorded_entry_fee = float(trade.get("entry_fee") or 0.0)
    recorded_exit_fee = float(trade.get("exit_fee") or 0.0)
    recorded_fees = max(0.0, recorded_entry_fee) + max(0.0, recorded_exit_fee)

    modeled_fees = round_trip_notional * fee_bps / 10000.0
    fee_shortfall = max(0.0, modeled_fees - recorded_fees)
    extra_slippage = round_trip_notional * slippage_bps / 10000.0

    return {
        "entry_notional": entry_notional,
        "exit_notional": exit_notional,
        "recorded_fees": recorded_fees,
        "modeled_fees": modeled_fees,
        "fee_shortfall": fee_shortfall,
        "extra_slippage": extra_slippage,
        "total_adjustment": fee_shortfall + extra_slippage,
    }


def _summary_from_pnls(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "win_rate": None,
            "total_pnl": 0.0,
            "average_pnl": None,
            "best_trade": None,
            "worst_trade": None,
            "profit_factor": None,
            "max_drawdown": 0.0,
        }

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakeven = len(pnls) - len(wins) - len(losses)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": breakeven,
        "win_rate": len(wins) / len(pnls),
        "total_pnl": sum(pnls),
        "average_pnl": sum(pnls) / len(pnls),
        "best_trade": max(pnls),
        "worst_trade": min(pnls),
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "max_drawdown": max_drawdown,
    }


def build_report(
    settings: Settings,
    db: TradingDB,
    *,
    limit: int | None,
    fee_bps: float,
    slippage_bps: float,
) -> dict[str, Any]:
    if fee_bps < 0 or slippage_bps < 0:
        raise ValueError("fee/slippage bps must be non-negative")

    trades = db.closed_trades(limit=limit, mode="testnet")
    trades = [
        trade
        for trade in trades
        if str(trade.get("symbol") or "") in set(settings.trading_symbols)
    ]
    trades.sort(key=lambda row: (str(row.get("closed_at") or ""), int(row["id"])))

    rows: list[dict[str, Any]] = []
    raw_pnls: list[float] = []
    fee_adjusted_pnls: list[float] = []
    stress_pnls: list[float] = []
    per_symbol: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"raw": [], "fee_adjusted": [], "stress": []}
    )
    exit_reasons: dict[str, int] = defaultdict(int)

    for trade in trades:
        raw_pnl = float(trade.get("pnl") or 0.0)
        costs = _scenario_cost(
            trade,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        fee_adjusted = raw_pnl - costs["fee_shortfall"]
        stress = fee_adjusted - costs["extra_slippage"]

        raw_pnls.append(raw_pnl)
        fee_adjusted_pnls.append(fee_adjusted)
        stress_pnls.append(stress)

        symbol = str(trade.get("symbol") or "")
        per_symbol[symbol]["raw"].append(raw_pnl)
        per_symbol[symbol]["fee_adjusted"].append(fee_adjusted)
        per_symbol[symbol]["stress"].append(stress)

        reason = str(trade.get("exit_reason") or "UNKNOWN")
        exit_reasons[reason] += 1

        rows.append({
            "id": int(trade["id"]),
            "symbol": symbol,
            "opened_at": trade.get("opened_at"),
            "closed_at": trade.get("closed_at"),
            "entry_price": float(trade.get("entry_price") or 0.0),
            "exit_price": float(trade.get("exit_price") or 0.0),
            "quantity": float(trade.get("quantity") or 0.0),
            "exit_reason": reason,
            "raw_pnl": raw_pnl,
            "raw_pnl_pct": float(trade.get("pnl_pct") or 0.0),
            "recorded_fees": costs["recorded_fees"],
            "modeled_fees": costs["modeled_fees"],
            "fee_adjusted_pnl": fee_adjusted,
            "stress_extra_slippage": costs["extra_slippage"],
            "stress_adjusted_pnl": stress,
        })

    symbol_summary = {}
    for symbol, buckets in sorted(per_symbol.items()):
        symbol_summary[symbol] = {
            "raw": _summary_from_pnls(buckets["raw"]),
            "fee_adjusted": _summary_from_pnls(buckets["fee_adjusted"]),
            "stress_adjusted": _summary_from_pnls(buckets["stress"]),
        }

    learning_progress = {}
    for symbol in settings.trading_symbols:
        symbol_closed = len(db.closed_trades(mode="testnet", symbol=symbol))
        learning_progress[symbol] = {
            "closed_trades": symbol_closed,
            "threshold": settings.min_trades_for_learning,
            "trades_until_learning": max(
                0, settings.min_trades_for_learning - symbol_closed
            ),
            "eligible": symbol_closed >= settings.min_trades_for_learning,
        }

    return {
        "mode": settings.mode,
        "symbols": settings.trading_symbols,
        "closed_trades": len(rows),
        "learning_threshold_per_symbol": settings.min_trades_for_learning,
        "learning_progress": learning_progress,
        "assumptions": {
            "fee_bps_per_side": fee_bps,
            "extra_slippage_bps_per_side": slippage_bps,
            "fee_method": (
                "Uses recorded fees when they are at least as large as the modeled fee; "
                "otherwise adds only the shortfall to reach the modeled fee."
            ),
            "slippage_method": (
                "Stress scenario subtracts the configured extra slippage on both entry "
                "and exit notionals. This is intentionally conservative."
            ),
        },
        "raw": _summary_from_pnls(raw_pnls),
        "fee_adjusted": _summary_from_pnls(fee_adjusted_pnls),
        "stress_adjusted": _summary_from_pnls(stress_pnls),
        "per_symbol": symbol_summary,
        "exit_reasons": dict(sorted(exit_reasons.items())),
        "trades": rows,
        "generated_at": datetime.now(UTC).isoformat(),
        "note": (
            "This report is descriptive Testnet analytics. It does not establish "
            "profitability or live-trading readiness."
        ),
    }


def main() -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(
        description="Summarize Binance Spot Testnet forward-trading performance."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only include the most recent N closed Testnet trades.",
    )
    parser.add_argument(
        "--fee-bps",
        type=float,
        default=settings.trading_fee_bps,
        help=(
            "Modeled fee per side in basis points when recorded Testnet fees are lower "
            "(default: TRADING_FEE_BPS from .env)."
        ),
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=2.0,
        help="Additional stress slippage per side in basis points (default: 2).",
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")

    db = TradingDB(settings.database_path)
    db.init()
    report = build_report(
        settings,
        db,
        limit=args.limit,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
