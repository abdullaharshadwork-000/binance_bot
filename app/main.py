import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.agents.orchestrator import TradingOrchestrator
from app.config import SUPPORTED_INTERVALS, get_settings
from app.strategy.indicators import ema
from app.ui.dashboard import DASHBOARD_HTML

settings = get_settings()
bot = TradingOrchestrator(settings)
app = FastAPI(title="Agentic Binance Bot", version="0.5.0")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@app.get("/health")
async def health():
    return {"ok": True, "mode": settings.mode, "running": bot.running}


async def _dashboard_payload() -> dict:
    market_monitor = bot.market_snapshot()
    price = market_monitor.get("price")
    market_error = None

    stale_limit_ms = settings.market_data_stale_seconds * 1000
    needs_rest_price = (
        price is None
        or market_monitor.get("price_age_ms") is None
        or float(market_monitor.get("price_age_ms") or 0) > stale_limit_ms
    )

    # When stopped, keep the dashboard price fresh without hammering Binance:
    # the refreshed REST price is cached in the orchestrator and reused until stale.
    if needs_rest_price:
        try:
            price = await bot.exchange.ticker_price(settings.symbol)
            now_ms = int(time.time() * 1000)
            bot.latest_price = price
            bot.latest_price_event_ms = now_ms
            bot.latest_price_received_monotonic = time.monotonic()
            bot.latest_price_source = "rest-dashboard"
            market_monitor = bot.market_snapshot()
        except Exception as exc:
            market_error = str(exc)

    open_trade = bot.db.get_open_trade(
        settings.symbol,
        settings.mode,
    )

    equity = bot.last_equity
    if price is not None:
        try:
            equity = await bot._poll_equity(price)
        except Exception as exc:
            market_error = market_error or str(exc)

    daily_pnl = bot.db.realized_pnl_today(
        mode=settings.mode,
        symbol=settings.symbol,
    )
    performance = bot.db.performance_summary(
        mode=settings.mode,
        symbol=settings.symbol,
    )
    learning = bot.learning.load().__dict__

    open_trade_metrics = None
    if open_trade and price is not None:
        qty = float(open_trade["quantity"])
        entry = float(open_trade["entry_price"])
        entry_fee = float(open_trade.get("entry_fee") or 0.0)
        estimated_exit_fee = price * qty * (settings.trading_fee_bps / 10_000)
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
        equity * settings.max_daily_loss_fraction
        if equity is not None else None
    )
    engine_error = None
    if isinstance(bot.last_cycle, dict):
        engine_error = bot.last_cycle.get("error") or bot.last_cycle.get("strategy_error")

    return {
        "running": bot.running,
        "price": price,
        "equity": equity,
        "daily_realized_pnl": daily_pnl,
        "daily_loss_limit_amount": daily_limit_amount,
        "market_error": market_error,
        "engine_error": engine_error,
        "market_monitor": market_monitor,
        "open_trade": open_trade,
        "open_trade_metrics": open_trade_metrics,
        "last_cycle": bot.last_cycle,
        "learning": learning,
        "performance": performance,
        "trades": bot.db.list_trades(
            20,
            mode=settings.mode,
            symbol=settings.symbol,
        ),
        "config": {
            "mode": settings.mode,
            "live_orders_allowed": (
                settings.mode == "live" and settings.allow_live_trading
            ),
            "symbol": settings.symbol,
            "interval": settings.interval,
            "cycle_seconds": settings.cycle_seconds,
            "use_websocket_market_data": settings.use_websocket_market_data,
            "market_data_stale_seconds": settings.market_data_stale_seconds,
            "account_refresh_seconds": settings.account_refresh_seconds,
            "paper_starting_balance": settings.paper_starting_balance,
            "risk_per_trade": settings.risk_per_trade,
            "max_position_fraction": settings.max_position_fraction,
            "max_daily_loss_fraction": settings.max_daily_loss_fraction,
            "stop_loss_pct": settings.stop_loss_pct,
            "take_profit_pct": settings.take_profit_pct,
            "min_signal_confidence": settings.min_signal_confidence,
            "trading_fee_bps": settings.trading_fee_bps,
            "paper_slippage_bps": settings.paper_slippage_bps,
            "adaptive_learning": settings.enable_adaptive_learning,
            "adaptive_live_allowed": settings.allow_adaptive_live,
            "min_trades_for_learning": settings.min_trades_for_learning,
            "llm_advisor": settings.enable_llm_advisor,
        },
    }


@app.get("/dashboard-data")
async def dashboard_data():
    return await _dashboard_payload()


@app.get("/status")
async def status():
    data = await _dashboard_payload()
    return {
        "running": data["running"],
        "mode": data["config"]["mode"],
        "live_orders_allowed": data["config"]["live_orders_allowed"],
        "symbol": data["config"]["symbol"],
        "price": data["price"],
        "equity": data["equity"],
        "market_monitor": data["market_monitor"],
        "open_trade": data["open_trade"],
        "last_cycle": data["last_cycle"],
        "learning": data["learning"],
        "engine_error": data["engine_error"],
    }


@app.post("/bot/run-once")
async def run_once():
    try:
        return await bot.run_once()
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/bot/start")
async def start_bot():
    try:
        started = await bot.start()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "started": started,
        "running": bot.running,
        "cycle_seconds": settings.cycle_seconds,
        "message": "Bot started" if started else "Bot was already running",
    }


@app.post("/bot/stop")
async def stop_bot():
    stopped = await bot.stop()
    return {
        "stopped": stopped,
        "running": bot.running,
        "message": "Bot stopped" if stopped else "Bot was already stopped",
    }


@app.get("/market/candles")
async def market_candles(interval: str | None = None, limit: int = 120):
    selected = interval or settings.interval
    if selected not in SUPPORTED_INTERVALS:
        raise HTTPException(status_code=400, detail="Unsupported candle interval")
    limit = max(30, min(limit, 300))
    try:
        frame = await bot.exchange.klines(settings.symbol, selected, limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    frame = frame.copy()
    frame["ema_fast"] = ema(frame["close"], 20)
    frame["ema_slow"] = ema(frame["close"], 50)

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
        })

    return {
        "symbol": settings.symbol,
        "interval": selected,
        "mode": settings.mode,
        "market_source": (
            "Binance Spot Testnet"
            if settings.mode == "testnet"
            else "Binance Spot"
        ),
        "candles": candles,
    }


@app.get("/trades")
async def trades(limit: int = 100):
    limit = max(1, min(limit, 500))
    return bot.db.list_trades(
        limit,
        mode=settings.mode,
        symbol=settings.symbol,
    )
