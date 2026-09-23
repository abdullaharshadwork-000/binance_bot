from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.agents.orchestrator import TradingOrchestrator
from app.config import get_settings
from app.ui.dashboard import DASHBOARD_HTML

settings = get_settings()
bot = TradingOrchestrator(settings)
app = FastAPI(title="Agentic Binance Bot", version="0.4.0")


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

    # When the engine is stopped there may be no stream yet. One REST read lets the
    # dashboard still display the real market price without creating a polling loop.
    if price is None:
        try:
            price = await bot.exchange.ticker_price(settings.symbol)
            market_monitor = {
                **market_monitor,
                "price": price,
                "source": "rest-dashboard",
                "price_age_ms": 0.0,
            }
        except Exception as exc:
            market_error = str(exc)

    open_trade = bot.db.get_open_trade(settings.symbol)
    equity = bot.last_equity
    if price is not None:
        try:
            if settings.mode == "paper":
                equity = await bot.broker.equity(price)
            elif equity is None and not bot.running:
                equity = await bot.broker.equity(price)
        except Exception as exc:
            market_error = market_error or str(exc)

    daily_pnl = bot.db.realized_pnl_today()
    performance = bot.db.performance_summary()
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
            "distance_to_stop_pct": (price - float(open_trade["stop_price"])) / price if price else None,
            "distance_to_take_profit_pct": (float(open_trade["take_profit_price"]) - price) / price if price else None,
        }

    daily_limit_amount = (equity * settings.max_daily_loss_fraction) if equity is not None else None

    return {
        "running": bot.running,
        "price": price,
        "equity": equity,
        "daily_realized_pnl": daily_pnl,
        "daily_loss_limit_amount": daily_limit_amount,
        "market_error": market_error,
        "market_monitor": market_monitor,
        "open_trade": open_trade,
        "open_trade_metrics": open_trade_metrics,
        "last_cycle": bot.last_cycle,
        "learning": learning,
        "performance": performance,
        "trades": bot.db.list_trades(20),
        "config": {
            "mode": settings.mode,
            "live_orders_allowed": settings.mode == "live" and settings.allow_live_trading,
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
    }


@app.post("/bot/run-once")
async def run_once():
    try:
        return await bot.run_once()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/bot/start")
async def start_bot():
    started = bot.start()
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
    allowed_intervals = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}
    selected = interval or settings.interval
    if selected not in allowed_intervals:
        raise HTTPException(status_code=400, detail="Unsupported candle interval")
    limit = max(30, min(limit, 300))
    try:
        frame = await bot.exchange.klines(settings.symbol, selected, limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

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
        })

    return {
        "symbol": settings.symbol,
        "interval": selected,
        "mode": settings.mode,
        "market_source": "Binance Spot Testnet" if settings.mode == "testnet" else "Binance Spot",
        "candles": candles,
    }


@app.get("/trades")
async def trades(limit: int = 100):
    limit = max(1, min(limit, 500))
    return bot.db.list_trades(limit)
