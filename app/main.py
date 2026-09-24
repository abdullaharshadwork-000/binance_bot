import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.agents.multi_orchestrator import MultiSymbolTradingManager
from app.config import SUPPORTED_INTERVALS, get_settings
from app.strategy.indicators import ema
from app.ui.dashboard import DASHBOARD_HTML

settings = get_settings()
manager = MultiSymbolTradingManager(settings)

# Backwards-compatible alias for code/tests that still import app.main.bot.
bot = manager.primary_bot

@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        await manager.stop()
        for item in manager.bots.values():
            await item.exchange.close()


app = FastAPI(title="Agentic Binance Bot", version="0.7.0", lifespan=lifespan)


def _selected_bot(symbol: str | None = None):
    try:
        return manager.get_bot(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@app.get("/health")
async def health():
    return {
        "ok": True,
        "mode": settings.mode,
        "running": manager.running,
        "symbols": manager.symbols,
        "running_symbols": [
            symbol for symbol, item in manager.bots.items() if item.running
        ],
    }


async def _dashboard_payload(symbol: str | None = None) -> dict:
    selected_bot = _selected_bot(symbol)
    selected_settings = selected_bot.settings

    market_monitor = selected_bot.market_snapshot()
    price = market_monitor.get("price")
    market_error = None

    stale_limit_ms = selected_settings.market_data_stale_seconds * 1000
    needs_rest_price = (
        price is None
        or market_monitor.get("price_age_ms") is None
        or float(market_monitor.get("price_age_ms") or 0) > stale_limit_ms
    )

    # When stopped, keep the selected dashboard price fresh without querying
    # every configured symbol on each 1-second dashboard refresh.
    if needs_rest_price:
        try:
            price = await selected_bot.exchange.ticker_price(selected_settings.symbol)
            now_ms = int(time.time() * 1000)
            selected_bot.latest_price = price
            selected_bot.latest_price_event_ms = now_ms
            selected_bot.latest_price_received_monotonic = time.monotonic()
            selected_bot.latest_price_source = "rest-dashboard"
            market_monitor = selected_bot.market_snapshot()
        except Exception as exc:
            market_error = str(exc)

    open_trade = selected_bot.db.get_open_trade(
        selected_settings.symbol,
        selected_settings.mode,
    )

    equity = selected_bot.last_equity
    if price is not None:
        try:
            equity = await selected_bot._poll_equity(price)
        except Exception as exc:
            market_error = market_error or str(exc)

    if len(manager.symbols) > 1:
        daily_pnl = manager.portfolio.realized_pnl_today()
    else:
        daily_pnl = selected_bot.db.realized_pnl_today(
            mode=selected_settings.mode,
            symbol=selected_settings.symbol,
        )

    performance = selected_bot.db.performance_summary(
        mode=selected_settings.mode,
        symbol=selected_settings.symbol,
    )
    learning = selected_bot.learning.load().__dict__

    open_trade_metrics = None
    if open_trade and price is not None:
        qty = float(open_trade["quantity"])
        entry = float(open_trade["entry_price"])
        entry_fee = float(open_trade.get("entry_fee") or 0.0)
        estimated_exit_fee = (
            price * qty * (selected_settings.trading_fee_bps / 10_000)
        )
        unrealized = (price - entry) * qty - entry_fee - estimated_exit_fee
        cost = entry * qty
        open_trade_metrics = {
            "unrealized_pnl": unrealized,
            "unrealized_pnl_pct": unrealized / cost if cost > 0 else 0.0,
            "estimated_exit_fee": estimated_exit_fee,
            "distance_to_stop_pct": (
                (price - float(open_trade["stop_price"])) / price
                if price else None
            ),
            "distance_to_take_profit_pct": (
                (float(open_trade["take_profit_price"]) - price) / price
                if price else None
            ),
        }

    daily_limit_amount = (
        equity * selected_settings.max_daily_loss_fraction
        if equity is not None else None
    )
    engine_error = selected_bot.reconciliation_warning or manager.portfolio.snapshot()["error"]
    if isinstance(selected_bot.last_cycle, dict):
        engine_error = (
            engine_error
            or selected_bot.last_cycle.get("error")
            or selected_bot.last_cycle.get("reconciliation_warning")
            or selected_bot.last_cycle.get("strategy_error")
        )

    return {
        "running": selected_bot.running,
        "portfolio_running": manager.running,
        "price": price,
        "equity": equity,
        "daily_realized_pnl": daily_pnl,
        "daily_loss_limit_amount": daily_limit_amount,
        "market_error": market_error,
        "engine_error": engine_error,
        "market_monitor": market_monitor,
        "open_trade": open_trade,
        "open_trade_metrics": open_trade_metrics,
        "last_cycle": selected_bot.last_cycle,
        "last_execution": selected_bot.last_execution,
        "learning": learning,
        "performance": performance,
        "trades": selected_bot.db.list_trades(
            20,
            mode=selected_settings.mode,
            symbol=selected_settings.symbol,
        ),
        "symbol_overview": manager.overview(),
        "portfolio": {
            **manager.portfolio.snapshot(),
            "open_positions": manager.portfolio.db.count_open_trades(
                mode=settings.mode,
                symbols=manager.symbols,
            ),
            "max_concurrent_positions": settings.max_concurrent_positions,
            "daily_realized_pnl": daily_pnl,
        },
        "config": {
            "mode": selected_settings.mode,
            "live_orders_allowed": (
                selected_settings.mode == "live"
                and selected_settings.allow_live_trading
            ),
            "multi_symbol_live_allowed": selected_settings.allow_multi_symbol_live,
            "symbol": selected_settings.symbol,
            "symbols": manager.symbols,
            "base_asset": selected_settings.base_asset,
            "quote_asset": selected_settings.quote_asset,
            "interval": selected_settings.interval,
            "supported_intervals": sorted(SUPPORTED_INTERVALS),
            "max_concurrent_positions": settings.max_concurrent_positions,
            "cycle_seconds": selected_settings.cycle_seconds,
            "use_websocket_market_data": selected_settings.use_websocket_market_data,
            "market_data_stale_seconds": selected_settings.market_data_stale_seconds,
            "account_refresh_seconds": selected_settings.account_refresh_seconds,
            "paper_starting_balance": selected_settings.paper_starting_balance,
            "risk_per_trade": selected_settings.risk_per_trade,
            "max_position_fraction": selected_settings.max_position_fraction,
            "max_daily_loss_fraction": selected_settings.max_daily_loss_fraction,
            "stop_loss_pct": selected_settings.stop_loss_pct,
            "take_profit_pct": selected_settings.take_profit_pct,
            "enable_trailing_stop": selected_settings.enable_trailing_stop,
            "breakeven_activation_pct": selected_settings.breakeven_activation_pct,
            "trailing_stop_activation_pct": selected_settings.trailing_stop_activation_pct,
            "trailing_stop_distance_pct": selected_settings.trailing_stop_distance_pct,
            "min_signal_confidence": selected_settings.min_signal_confidence,
            "strategy_min_volume_ratio": selected_settings.strategy_min_volume_ratio,
            "strategy_max_extension_atr": selected_settings.strategy_max_extension_atr,
            "trading_fee_bps": selected_settings.trading_fee_bps,
            "paper_slippage_bps": selected_settings.paper_slippage_bps,
            "adaptive_learning": selected_settings.enable_adaptive_learning,
            "adaptive_live_allowed": selected_settings.allow_adaptive_live,
            "min_trades_for_learning": selected_settings.min_trades_for_learning,
            "llm_advisor": selected_settings.enable_llm_advisor,
        },
    }


@app.get("/dashboard-data")
async def dashboard_data(symbol: str | None = None):
    return await _dashboard_payload(symbol)


@app.get("/status")
async def status(symbol: str | None = None):
    data = await _dashboard_payload(symbol)
    return {
        "running": data["running"],
        "portfolio_running": data["portfolio_running"],
        "mode": data["config"]["mode"],
        "live_orders_allowed": data["config"]["live_orders_allowed"],
        "symbol": data["config"]["symbol"],
        "symbols": data["config"]["symbols"],
        "price": data["price"],
        "equity": data["equity"],
        "market_monitor": data["market_monitor"],
        "open_trade": data["open_trade"],
        "last_cycle": data["last_cycle"],
        "last_execution": data["last_execution"],
        "symbol_overview": data["symbol_overview"],
        "portfolio": data["portfolio"],
        "learning": data["learning"],
        "engine_error": data["engine_error"],
    }


@app.post("/bot/analyze")
async def analyze_market(symbol: str | None = None):
    selected_bot = _selected_bot(symbol)
    try:
        return await selected_bot.analyze_once()
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/bot/analyze-all")
async def analyze_all_markets():
    if manager.running:
        raise HTTPException(
            status_code=409,
            detail="Stop the automated bot before running read-only analysis",
        )
    return await manager.analyze_all()


@app.post("/bot/run-once")
async def run_once(symbol: str | None = None):
    selected_bot = _selected_bot(symbol)
    try:
        return await selected_bot.run_once()
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/bot/start")
async def start_bot():
    try:
        started = await manager.start()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "started": started,
        "running": manager.running,
        "symbols": manager.symbols,
        "cycle_seconds": settings.cycle_seconds,
        "message": "Multi-symbol bot started",
    }


@app.post("/bot/stop")
async def stop_bot():
    stopped = await manager.stop()
    return {
        "stopped": stopped,
        "running": manager.running,
        "symbols": manager.symbols,
        "message": "Multi-symbol bot stopped",
    }


@app.get("/market/candles")
async def market_candles(
    interval: str | None = None,
    limit: int = 120,
    symbol: str | None = None,
):
    selected_bot = _selected_bot(symbol)
    selected_settings = selected_bot.settings
    selected_interval = interval or selected_settings.interval
    if selected_interval not in SUPPORTED_INTERVALS:
        raise HTTPException(status_code=400, detail="Unsupported candle interval")
    limit = max(30, min(limit, 300))
    history_limit = max(250, limit)
    try:
        frame = await selected_bot.exchange.klines(
            selected_settings.symbol,
            selected_interval,
            history_limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    frame = frame.copy()
    now_ms = int(time.time() * 1000)
    frame["ema_fast"] = ema(frame["close"], 20)
    frame["ema_slow"] = ema(frame["close"], 50)
    frame = frame.tail(limit)

    candles = []
    for row in frame.to_dict(orient="records"):
        candles.append({
            "open_time": int(row["open_time"]),
            "close_time": int(row["close_time"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "ema_fast": (
                float(row["ema_fast"])
                if row["ema_fast"] == row["ema_fast"] else None
            ),
            "ema_slow": (
                float(row["ema_slow"])
                if row["ema_slow"] == row["ema_slow"] else None
            ),
            "is_closed": int(row["close_time"]) < now_ms,
        })

    return {
        "symbol": selected_settings.symbol,
        "interval": selected_interval,
        "mode": selected_settings.mode,
        "market_source": (
            "Binance Spot Testnet"
            if selected_settings.mode == "testnet"
            else "Binance Spot"
        ),
        "candles": candles,
    }


@app.get("/trades")
async def trades(limit: int = 100, symbol: str | None = None):
    selected_bot = _selected_bot(symbol)
    selected_settings = selected_bot.settings
    limit = max(1, min(limit, 500))
    return selected_bot.db.list_trades(
        limit,
        mode=selected_settings.mode,
        symbol=selected_settings.symbol,
    )
