from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from app.config import Settings
from app.models import SignalSide
from app.risk.manager import RiskManager
from app.storage.db import TradingDB
from app.strategy.ensemble import EnsembleStrategy

INTERVALS = ("1m", "3m", "5m", "15m")
PUBLIC_BINANCE = "https://api.binance.com"
FETCH_RETRIES = 5
CACHE_MAX_AGE_SECONDS = 30 * 60
CACHE_DIR = Path("data/strategy_interval_cache")


def symbols_for_comparison(settings: Settings) -> list[str]:
    """Use the same persisted dynamic market set as the trading engine."""
    if settings.universe_mode != "top_volume":
        return settings.trading_symbols

    saved_symbols: list[str] = []
    tracked_symbols: list[str] = []
    database_path = Path(settings.database_path)
    if database_path.is_file():
        # Read the live engine's last selected universe without initializing or
        # migrating its database as a side effect of a research run.
        db = TradingDB(str(database_path))
        saved_symbols = db.saved_trading_universe(mode=settings.mode)
        tracked_symbols = db.tracked_symbols(mode=settings.mode)

    symbols = [
        *saved_symbols,
        *settings.configured_symbols,
        *settings.pinned_trading_symbols,
        *tracked_symbols,
    ]
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        raise RuntimeError(
            "No dynamic universe is recorded yet. Discover the market universe "
            "from the dashboard first, or add manual pins before backtesting."
        )
    return symbols


def interval_to_ms(interval: str) -> int:
    amount = int(interval[:-1])
    unit = interval[-1]
    factors = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    if unit not in factors:
        raise ValueError(f"Unsupported interval: {interval}")
    return amount * factors[unit]


def _cache_path(symbol: str, interval: str, days: int) -> Path:
    return CACHE_DIR / f"{symbol}_{interval}_{days}d.json"


def _load_history_checkpoint(
    *, symbol: str, interval: str, days: int, now_ms: int
) -> tuple[list[dict[str, Any]], int | None, int | None, bool]:
    path = _cache_path(symbol, interval, days)
    if not path.exists():
        return [], None, None, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        end_ms = int(payload["end_ms"])
        if abs(now_ms - end_ms) > CACHE_MAX_AGE_SECONDS * 1000:
            return [], None, None, False
        rows = list(payload.get("rows") or [])
        cursor = int(payload.get("cursor") or 0) or None
        complete = bool(payload.get("complete"))
        return rows, cursor, end_ms, complete
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return [], None, None, False


def _save_history_checkpoint(
    *,
    symbol: str,
    interval: str,
    days: int,
    end_ms: int,
    cursor: int,
    rows: list[dict[str, Any]],
    complete: bool,
) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol, interval, days)
    tmp = path.with_suffix(".tmp")
    payload = {
        "end_ms": end_ms,
        "cursor": cursor,
        "complete": complete,
        "rows": rows,
    }
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


async def _get_klines_with_retry(
    client: httpx.AsyncClient,
    *,
    params: dict[str, Any],
    symbol: str,
    interval: str,
    retries: int = FETCH_RETRIES,
) -> list:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = await client.get("/api/v3/klines", params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            last_error = exc
            retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                exc.response.status_code == 429 or exc.response.status_code >= 500
            )
            if not retryable or attempt >= retries:
                break
            delay = min(2 ** (attempt - 1), 8)
            print(
                f"[{symbol} {interval}] Binance request failed "
                f"(attempt {attempt}/{retries}: {type(exc).__name__}). "
                f"Retrying in {delay}s...",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(delay)

    raise RuntimeError(
        f"Could not download {symbol} {interval} candles after {retries} attempts. "
        "The partial download was saved, so run the command again to resume."
    ) from last_error


async def fetch_history(
    client: httpx.AsyncClient,
    *,
    symbol: str,
    interval: str,
    days: int,
) -> pd.DataFrame:
    """Fetch public Binance Spot candles with retry and resumable local checkpoints."""
    now_ms = int(time.time() * 1000)
    rows, cached_cursor, cached_end_ms, complete = _load_history_checkpoint(
        symbol=symbol,
        interval=interval,
        days=days,
        now_ms=now_ms,
    )

    if cached_end_ms is not None:
        end_ms = cached_end_ms
        start_ms = end_ms - days * 86_400_000
        cursor = cached_cursor or start_ms
        if complete and rows:
            print(
                f"[{symbol} {interval}] using cached {len(rows)} candles",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"[{symbol} {interval}] resuming partial download "
                f"({len(rows)} candles cached)",
                file=sys.stderr,
                flush=True,
            )
    else:
        end_ms = now_ms
        start_ms = end_ms - days * 86_400_000
        cursor = start_ms
        rows = []

    if not complete:
        while cursor < end_ms:
            batch = await _get_klines_with_retry(
                client,
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
                symbol=symbol,
                interval=interval,
            )
            if not batch:
                complete = True
                break

            for k in batch:
                rows.append(
                    {
                        "open_time": int(k[0]),
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "close_time": int(k[6]),
                    }
                )

            last_open = int(batch[-1][0])
            next_cursor = last_open + interval_to_ms(interval)
            if next_cursor <= cursor:
                complete = True
                break
            cursor = next_cursor

            complete = len(batch) < 1000 or cursor >= end_ms
            _save_history_checkpoint(
                symbol=symbol,
                interval=interval,
                days=days,
                end_ms=end_ms,
                cursor=cursor,
                rows=rows,
                complete=complete,
            )
            print(
                f"[{symbol} {interval}] downloaded {len(rows)} candles",
                file=sys.stderr,
                flush=True,
            )
            if complete:
                break

    if not rows:
        return pd.DataFrame(
            columns=["open_time", "open", "high", "low", "close", "volume", "close_time"]
        )

    frame = pd.DataFrame(rows).drop_duplicates("open_time").sort_values("open_time")
    frame = frame[frame["close_time"] < int(time.time() * 1000)].reset_index(drop=True)
    return frame


@dataclass
class Position:
    entry_market: float
    entry_fill: float
    entry_time: int
    stop: float
    take_profit: float
    high_water: float
    stop_pct: float
    take_profit_pct: float


def summarize(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "total_pnl": 0.0,
            "average_pnl": None,
            "profit_factor": None,
            "max_drawdown": 0.0,
        }

    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
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
        "win_rate": len(wins) / len(pnls),
        "total_pnl": sum(pnls),
        "average_pnl": sum(pnls) / len(pnls),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "max_drawdown": max_drawdown,
    }


def trade_costs(
    *,
    entry_market: float,
    exit_market: float,
    notional: float,
    fee_bps: float,
    slippage_bps: float,
    extra_stress_slippage_bps: float,
) -> dict[str, float]:
    fee_rate = fee_bps / 10_000
    slip_rate = slippage_bps / 10_000
    stress_rate = extra_stress_slippage_bps / 10_000

    raw_qty = notional / entry_market
    raw_pnl = raw_qty * (exit_market - entry_market)

    raw_exit_notional = raw_qty * exit_market
    fee_adjusted = raw_pnl - (notional + raw_exit_notional) * fee_rate

    entry_fill = entry_market * (1 + slip_rate)
    qty = notional / entry_fill
    exit_fill = exit_market * (1 - slip_rate)
    exit_notional = qty * exit_fill
    cost_adjusted = exit_notional - notional - (notional + exit_notional) * fee_rate

    stress_entry_fill = entry_market * (1 + slip_rate + stress_rate)
    stress_qty = notional / stress_entry_fill
    stress_exit_fill = exit_market * (1 - slip_rate - stress_rate)
    stress_exit_notional = stress_qty * stress_exit_fill
    stress_adjusted = (
        stress_exit_notional
        - notional
        - (notional + stress_exit_notional) * fee_rate
    )

    return {
        "raw_pnl": raw_pnl,
        "fee_adjusted_pnl": fee_adjusted,
        "cost_adjusted_pnl": cost_adjusted,
        "stress_adjusted_pnl": stress_adjusted,
    }


def backtest_frame(
    frame: pd.DataFrame,
    *,
    settings: Settings,
    interval: str,
    symbol: str,
    notional: float,
    extra_stress_slippage_bps: float,
) -> dict[str, Any]:
    if len(frame) < 62:
        return {
            "symbol": symbol,
            "interval": interval,
            "candles": len(frame),
            "trades": [],
            "raw": summarize([]),
            "fee_adjusted": summarize([]),
            "cost_adjusted": summarize([]),
            "stress_adjusted": summarize([]),
        }

    strategy = EnsembleStrategy(
        min_trend_atr=settings.strategy_min_trend_atr,
        min_adx=settings.strategy_min_adx,
        min_volume_ratio=settings.strategy_min_volume_ratio,
        max_extension_atr=settings.strategy_max_extension_atr,
    )

    slip_rate = settings.paper_slippage_bps / 10_000
    fee_rate = settings.trading_fee_bps / 10_000
    threshold = settings.min_signal_confidence

    position: Position | None = None
    pending_entry = False
    # Stop/target fractions (fractions of price) captured from the qualifying
    # signal candle. ATR-scaled when enabled, mirroring the live RiskManager.
    pending_stop_pcts: tuple[float, float] | None = None
    pending_strategy_exit = False
    last_exit_time: int | None = None
    trades: list[dict[str, Any]] = []
    risk_manager = RiskManager(settings)

    def close_position(exit_market: float, exit_time: int, reason: str) -> None:
        nonlocal position, pending_strategy_exit, last_exit_time
        assert position is not None
        costs = trade_costs(
            entry_market=position.entry_market,
            exit_market=exit_market,
            notional=notional,
            fee_bps=settings.trading_fee_bps,
            slippage_bps=settings.paper_slippage_bps,
            extra_stress_slippage_bps=extra_stress_slippage_bps,
        )
        trades.append(
            {
                "entry_time": position.entry_time,
                "exit_time": exit_time,
                "entry_price": position.entry_market,
                "exit_price": exit_market,
                "reason": reason,
                "stop_pct": position.stop_pct,
                "take_profit_pct": position.take_profit_pct,
                **costs,
            }
        )
        position = None
        pending_strategy_exit = False
        last_exit_time = exit_time

    for i in range(60, len(frame)):
        candle = frame.iloc[i]

        if pending_strategy_exit and position is not None:
            close_position(float(candle.open), int(candle.open_time), "Strategy exit")
            continue

        if pending_entry and position is None:
            if (
                last_exit_time is None
                or int(candle.open_time) - last_exit_time
                >= int(settings.entry_cooldown_seconds * 1000)
            ):
                entry_market = float(candle.open)
                entry_fill = entry_market * (1 + slip_rate)
                stop_pct, take_pct = pending_stop_pcts or (
                    settings.stop_loss_pct,
                    settings.take_profit_pct,
                )
                position = Position(
                    entry_market=entry_market,
                    entry_fill=entry_fill,
                    entry_time=int(candle.open_time),
                    stop=entry_fill * (1 - stop_pct),
                    take_profit=entry_fill * (1 + take_pct),
                    high_water=entry_fill,
                    stop_pct=stop_pct,
                    take_profit_pct=take_pct,
                )
            pending_entry = False
            pending_stop_pcts = None

        if position is not None:
            low = float(candle.low)
            high = float(candle.high)

            stop_hit = low <= position.stop
            take_hit = high >= position.take_profit
            if stop_hit and take_hit:
                # OHLC data cannot reveal which happened first; use the conservative outcome.
                close_position(position.stop, int(candle.close_time), "Stop loss (ambiguous candle)")
                continue
            if stop_hit:
                close_position(position.stop, int(candle.close_time), "Stop loss")
                continue
            if take_hit:
                close_position(position.take_profit, int(candle.close_time), "Take profit")
                continue

        history = frame.iloc[: i + 1][["open", "high", "low", "close", "volume"]]
        signal = strategy.evaluate(history, has_position=position is not None)

        if position is not None:
            if signal.side == SignalSide.SELL:
                pending_strategy_exit = True
            elif settings.enable_trailing_stop:
                high_water = max(position.high_water, float(candle.high))
                candidate_stop = position.stop

                if high_water >= position.entry_fill * (1 + settings.breakeven_activation_pct):
                    unit_cost = position.entry_fill
                    breakeven = unit_cost / ((1 - fee_rate) * (1 - slip_rate))
                    if breakeven < float(candle.close):
                        candidate_stop = max(candidate_stop, breakeven)

                if high_water >= position.entry_fill * (1 + settings.trailing_stop_activation_pct):
                    candidate_stop = max(
                        candidate_stop,
                        high_water * (1 - settings.trailing_stop_distance_pct),
                    )

                position.high_water = high_water
                position.stop = candidate_stop

        elif (
            signal.side == SignalSide.BUY
            and signal.confidence >= threshold
            and i + 1 < len(frame)
        ):
            pending_entry = True
            pending_stop_pcts = risk_manager.atr_scaled_stop_distances(signal)

    if position is not None:
        close_position(float(frame.iloc[-1].close), int(frame.iloc[-1].close_time), "End of sample")

    raw = [float(t["raw_pnl"]) for t in trades]
    fee_adjusted = [float(t["fee_adjusted_pnl"]) for t in trades]
    cost_adjusted = [float(t["cost_adjusted_pnl"]) for t in trades]
    stress_adjusted = [float(t["stress_adjusted_pnl"]) for t in trades]

    return {
        "symbol": symbol,
        "interval": interval,
        "candles": len(frame),
        "trades": trades,
        "raw": summarize(raw),
        "fee_adjusted": summarize(fee_adjusted),
        "cost_adjusted": summarize(cost_adjusted),
        "stress_adjusted": summarize(stress_adjusted),
    }


def combine_symbol_results(results: list[dict[str, Any]], interval: str) -> dict[str, Any]:
    trades = []
    for result in results:
        for trade in result["trades"]:
            trades.append({"symbol": result["symbol"], **trade})
    trades.sort(key=lambda row: (row["exit_time"], row["symbol"]))

    return {
        "interval": interval,
        "trades": len(trades),
        "raw": summarize([float(t["raw_pnl"]) for t in trades]),
        "fee_adjusted": summarize([float(t["fee_adjusted_pnl"]) for t in trades]),
        "cost_adjusted": summarize([float(t["cost_adjusted_pnl"]) for t in trades]),
        "stress_adjusted": summarize([float(t["stress_adjusted_pnl"]) for t in trades]),
        "per_symbol": {
            result["symbol"]: {
                "trades": len(result["trades"]),
                "raw": result["raw"],
                "fee_adjusted": result["fee_adjusted"],
                "cost_adjusted": result["cost_adjusted"],
                "stress_adjusted": result["stress_adjusted"],
            }
            for result in results
        },
    }


async def build_comparison(
    settings: Settings,
    *,
    days: int,
    intervals: tuple[str, ...],
    notional: float,
    extra_stress_slippage_bps: float,
) -> dict[str, Any]:
    symbols = symbols_for_comparison(settings)
    timeout = httpx.Timeout(connect=15.0, read=60.0, write=20.0, pool=20.0)
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
    async with httpx.AsyncClient(
        base_url=PUBLIC_BINANCE,
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
    ) as client:
        comparison = {}
        for interval in intervals:
            symbol_results = []
            for symbol in symbols:
                print(
                    f"Fetching {symbol} {interval} ({days} days)...",
                    file=sys.stderr,
                    flush=True,
                )
                frame = await fetch_history(
                    client,
                    symbol=symbol,
                    interval=interval,
                    days=days,
                )
                symbol_results.append(
                    backtest_frame(
                        frame,
                        settings=settings,
                        interval=interval,
                        symbol=symbol,
                        notional=notional,
                        extra_stress_slippage_bps=extra_stress_slippage_bps,
                    )
                )
            comparison[interval] = combine_symbol_results(symbol_results, interval)

    candidates = [
        (interval, data["stress_adjusted"]["total_pnl"])
        for interval, data in comparison.items()
        if data["trades"] > 0
    ]

    return {
        "source": "Binance Spot public historical klines",
        "symbols": symbols,
        "intervals": list(intervals),
        "days": days,
        "notional_per_trade": notional,
        "strategy_parameters": {
            "min_signal_confidence": settings.min_signal_confidence,
            "stop_loss_pct": settings.stop_loss_pct,
            "take_profit_pct": settings.take_profit_pct,
            "use_atr_stops": settings.use_atr_stops,
            "atr_stop_multiplier": settings.atr_stop_multiplier,
            "atr_stop_min_pct": settings.atr_stop_min_pct,
            "atr_stop_max_pct": settings.atr_stop_max_pct,
            "trailing_stop_enabled": settings.enable_trailing_stop,
            "breakeven_activation_pct": settings.breakeven_activation_pct,
            "trailing_stop_activation_pct": settings.trailing_stop_activation_pct,
            "trailing_stop_distance_pct": settings.trailing_stop_distance_pct,
            "strategy_min_trend_atr": settings.strategy_min_trend_atr,
            "strategy_min_adx": settings.strategy_min_adx,
            "strategy_min_volume_ratio": settings.strategy_min_volume_ratio,
            "strategy_max_extension_atr": settings.strategy_max_extension_atr,
        },
        "cost_assumptions": {
            "fee_bps_per_side": settings.trading_fee_bps,
            "baseline_slippage_bps_per_side": settings.paper_slippage_bps,
            "extra_stress_slippage_bps_per_side": extra_stress_slippage_bps,
        },
        "comparison": comparison,
        "best_stress_adjusted_interval": (
            max(candidates, key=lambda item: item[1])[0] if candidates else None
        ),
        "limitations": [
            "This is a candle-based research backtest, not an execution simulator.",
            "Entries execute at the next candle open after a qualifying completed-candle BUY signal.",
            "Strategy exits execute at the next candle open.",
            "Protective stop/take-profit checks use OHLC data; if both are touched in one candle, the stop is assumed first.",
            "Trailing and breakeven stops are ratcheted after a candle and apply from the next candle to avoid intrabar look-ahead.",
            "The comparison tests symbols independently and does not model the live multi-symbol portfolio position cap.",
            "Historical performance does not establish future profitability.",
        ],
    }


WF_GRID: dict[str, tuple[float, ...]] = {
    "strategy_min_adx": (12.0, 16.0, 20.0),
    "strategy_min_volume_ratio": (0.6, 0.75, 0.9),
    "use_atr_stops": (False, True),
}
WF_MIN_SEGMENT_CANDLES = 62  # backtest_frame warmup (60) + minimum evaluation room


def _wf_variant(settings: Settings, overrides: dict[str, float]) -> Settings:
    return settings.model_copy(update=overrides)


def _wf_slices(total: int, folds: int, warmup: int = 60) -> list[tuple[int, int]]:
    """Split the usable candle range (after warmup) into `folds` contiguous segments."""
    usable = total - warmup
    seg = usable // folds
    return [
        (warmup + i * seg, warmup + (i + 1) * seg if i < folds - 1 else total)
        for i in range(folds)
    ]


def walk_forward_symbol(
    frame: pd.DataFrame,
    *,
    settings: Settings,
    interval: str,
    symbol: str,
    notional: float,
    extra_stress_slippage_bps: float,
    folds: int,
    grid: dict[str, tuple[float, ...]] | None = None,
) -> dict[str, Any]:
    """Classic walk-forward: optimize parameters on one fold, trade the next.

    For each adjacent fold pair, the grid is scored on the training segment
    (stress-adjusted PnL) and the winning parameters are evaluated untouched
    on the following test segment. The current configured parameters are also
    evaluated out-of-sample as a baseline, so the report shows whether
    optimization adds anything over doing nothing clever.
    """
    grid = grid or WF_GRID
    keys = sorted(grid)
    combos = [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(grid[k] for k in keys))
    ]
    slices = _wf_slices(len(frame), folds)

    fold_reports: list[dict[str, Any]] = []
    optimized_oos_total = 0.0
    optimized_oos_trades = 0
    baseline_oos_total = 0.0
    baseline_oos_trades = 0

    for f in range(1, len(slices)):
        train_start, train_end = slices[f - 1]
        test_start, test_end = slices[f]
        train = frame.iloc[train_start:train_end]
        test = frame.iloc[test_start:test_end]
        if len(train) < WF_MIN_SEGMENT_CANDLES or len(test) < WF_MIN_SEGMENT_CANDLES:
            fold_reports.append({
                "fold": f,
                "skipped": "Segment too short for a reliable simulation",
            })
            continue

        scored = []
        for combo in combos:
            variant = _wf_variant(settings, combo)
            result = backtest_frame(
                train,
                settings=variant,
                interval=interval,
                symbol=symbol,
                notional=notional,
                extra_stress_slippage_bps=extra_stress_slippage_bps,
            )
            scored.append((result["stress_adjusted"]["total_pnl"], result["trades"], combo))

        best_pnl, best_is_trades, best_combo = max(scored, key=lambda item: item[0])
        oos = backtest_frame(
            test,
            settings=_wf_variant(settings, best_combo),
            interval=interval,
            symbol=symbol,
            notional=notional,
            extra_stress_slippage_bps=extra_stress_slippage_bps,
        )
        baseline = backtest_frame(
            test,
            settings=settings,
            interval=interval,
            symbol=symbol,
            notional=notional,
            extra_stress_slippage_bps=extra_stress_slippage_bps,
        )
        optimized_oos_total += oos["stress_adjusted"]["total_pnl"]
        optimized_oos_trades += oos["stress_adjusted"]["trades"]
        baseline_oos_total += baseline["stress_adjusted"]["total_pnl"]
        baseline_oos_trades += baseline["stress_adjusted"]["trades"]
        fold_reports.append({
            "fold": f,
            "train_candles": len(train),
            "test_candles": len(test),
            "best_params": best_combo,
            "in_sample_stress_pnl": round(best_pnl, 4),
            "in_sample_trades": best_is_trades,
            "out_of_sample_stress_pnl": round(oos["stress_adjusted"]["total_pnl"], 4),
            "out_of_sample_trades": oos["stress_adjusted"]["trades"],
            "baseline_out_of_sample_stress_pnl": round(
                baseline["stress_adjusted"]["total_pnl"], 4
            ),
        })

    return {
        "symbol": symbol,
        "interval": interval,
        "candles": len(frame),
        "folds": folds,
        "grid": {key: list(values) for key, values in grid.items()},
        "fold_reports": fold_reports,
        "optimized_oos_stress_pnl": round(optimized_oos_total, 4),
        "optimized_oos_trades": optimized_oos_trades,
        "baseline_oos_stress_pnl": round(baseline_oos_total, 4),
        "baseline_oos_trades": baseline_oos_trades,
    }


async def build_walk_forward(
    settings: Settings,
    *,
    days: int,
    interval: str,
    notional: float,
    extra_stress_slippage_bps: float,
    folds: int,
    grid: dict[str, tuple[float, ...]] | None = None,
) -> dict[str, Any]:
    symbols = symbols_for_comparison(settings)
    timeout = httpx.Timeout(connect=15.0, read=60.0, write=20.0, pool=20.0)
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
    async with httpx.AsyncClient(
        base_url=PUBLIC_BINANCE,
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
    ) as client:
        results = []
        for symbol in symbols:
            print(f"Fetching {symbol} {interval} ({days} days)...", file=sys.stderr, flush=True)
            frame = await fetch_history(client, symbol=symbol, interval=interval, days=days)
            print(f"Walk-forward on {symbol} ({len(frame)} candles)...", file=sys.stderr, flush=True)
            results.append(
                walk_forward_symbol(
                    frame,
                    settings=settings,
                    interval=interval,
                    symbol=symbol,
                    notional=notional,
                    extra_stress_slippage_bps=extra_stress_slippage_bps,
                    folds=folds,
                    grid=grid,
                )
            )

    optimized = sum(item["optimized_oos_stress_pnl"] for item in results)
    baseline = sum(item["baseline_oos_stress_pnl"] for item in results)
    return {
        "source": "Binance Spot public historical klines",
        "mode": "walk_forward",
        "symbols": symbols,
        "interval": interval,
        "days": days,
        "notional_per_trade": notional,
        "grid": {key: list(values) for key, values in (grid or WF_GRID).items()},
        "folds": folds,
        "totals": {
            "optimized_out_of_sample_stress_pnl": round(optimized, 4),
            "baseline_out_of_sample_stress_pnl": round(baseline, 4),
            "advantage_of_optimization": round(optimized - baseline, 4),
        },
        "symbols_results": results,
        "limitations": [
            "Walk-forward optimization re-fits parameters per fold; it reduces but does not remove overfitting risk.",
            "A positive out-of-sample result on a few folds is weak evidence; many folds and regimes are required.",
            "This is a candle-based research backtest, not an execution simulator; entries/exits assume the next candle open.",
            "Historical performance does not establish future profitability or live-readiness.",
        ],
    }


def main() -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(
        description="Compare the current strategy across multiple Binance Spot candle intervals."
    )
    parser.add_argument("--days", type=int, default=7, help="Historical lookback in days (default: 7).")
    parser.add_argument(
        "--intervals",
        default=",".join(INTERVALS),
        help="Comma-separated intervals (default: 1m,3m,5m,15m).",
    )
    parser.add_argument(
        "--notional",
        type=float,
        default=3000.0,
        help="Fixed quote notional per simulated trade (default: 3000 USDT).",
    )
    parser.add_argument(
        "--stress-slippage-bps",
        type=float,
        default=2.0,
        help="Extra slippage per side for the stress scenario (default: 2 bps).",
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help=(
            "Optimize the entry filters on each training fold and evaluate them "
            "on the next fold (single interval; see --wf-interval)."
        ),
    )
    parser.add_argument(
        "--wf-folds",
        type=int,
        default=3,
        help="Number of contiguous walk-forward folds (default: 3).",
    )
    parser.add_argument(
        "--wf-interval",
        default="15m",
        help="Interval used for walk-forward mode (default: 15m).",
    )
    args = parser.parse_args()

    if args.days <= 0:
        parser.error("--days must be positive")
    if args.notional <= 0 or not math.isfinite(args.notional):
        parser.error("--notional must be a positive finite number")
    if args.stress_slippage_bps < 0 or not math.isfinite(args.stress_slippage_bps):
        parser.error("--stress-slippage-bps must be non-negative")
    if args.wf_folds < 2 or args.wf_folds > 10:
        parser.error("--wf-folds must be between 2 and 10")

    intervals = tuple(x.strip() for x in args.intervals.split(",") if x.strip())
    unsupported = [x for x in intervals if x not in INTERVALS]
    if unsupported:
        parser.error(
            "This comparison tool supports: "
            + ", ".join(INTERVALS)
            + ". Unsupported: "
            + ", ".join(unsupported)
        )

    if args.walk_forward:
        wf_interval = args.wf_interval.strip()
        if wf_interval not in INTERVALS:
            parser.error(
                "--wf-interval must be one of: " + ", ".join(INTERVALS)
            )
        report = asyncio.run(
            build_walk_forward(
                settings,
                days=args.days,
                interval=wf_interval,
                notional=args.notional,
                extra_stress_slippage_bps=args.stress_slippage_bps,
                folds=args.wf_folds,
            )
        )
        print(json.dumps(report, indent=2))
        return 0

    report = asyncio.run(
        build_comparison(
            settings,
            days=args.days,
            intervals=intervals,
            notional=args.notional,
            extra_stress_slippage_bps=args.stress_slippage_bps,
        )
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
