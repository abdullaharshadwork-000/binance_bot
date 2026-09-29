from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.exchange.binance import BinanceClient
from app.execution.broker import Broker
from app.models import RiskDecision, SignalSide, StrategySignal
from app.runtime_lock import RuntimeFileLock
from app.storage.db import TradingDB


def build_validation_settings(base: Settings, database_path: str) -> Settings:
    return Settings(
        _env_file=None,
        mode="testnet",
        allow_live_trading=False,
        live_protection_validated=False,
        symbol=base.symbol,
        base_asset=base.base_asset,
        quote_asset=base.quote_asset,
        symbols=base.symbol,
        interval=base.interval,
        binance_api_key=base.binance_api_key,
        binance_api_secret=base.binance_api_secret,
        database_path=database_path,
        learning_profile_path=str(Path(database_path).with_suffix(".learning.json")),
        risk_per_trade=base.risk_per_trade,
        max_position_fraction=base.max_position_fraction,
        max_daily_loss_fraction=base.max_daily_loss_fraction,
        stop_loss_pct=base.stop_loss_pct,
        take_profit_pct=base.take_profit_pct,
        enable_trailing_stop=base.enable_trailing_stop,
        trailing_stop_activation_pct=base.trailing_stop_activation_pct,
        trailing_stop_distance_pct=base.trailing_stop_distance_pct,
        breakeven_activation_pct=base.breakeven_activation_pct,
        trading_fee_bps=base.trading_fee_bps,
        max_entry_spread_bps=base.max_entry_spread_bps,
        max_entry_slippage_bps=base.max_entry_slippage_bps,
        account_refresh_seconds=base.account_refresh_seconds,
        account_max_age_seconds=base.account_max_age_seconds,
    )


async def run_validation(quote_amount: float, database_path: str) -> dict:
    base = Settings()
    if base.mode != "testnet":
        raise RuntimeError("Validation requires MODE=testnet in .env")
    if quote_amount <= 0:
        raise ValueError("--quote-amount must be positive")

    process_lock = RuntimeFileLock(base.database_path)
    process_lock.acquire()
    exchange: BinanceClient | None = None
    restarted_exchange: BinanceClient | None = None
    try:
        settings = build_validation_settings(base, database_path)
        settings.ensure_directories()
        db = TradingDB(settings.database_path)
        db.init()
        if db.get_open_trade(settings.symbol, "testnet"):
            raise RuntimeError(
                f"Validation database already has an open {settings.symbol} position; "
                "resolve it before running again"
            )

        exchange = BinanceClient(settings)
        stage = "ping"
        await exchange.ping()
        stage = "symbol_validation"
        await exchange.validate_symbol_assets(
            settings.symbol,
            settings.base_asset,
            settings.quote_asset,
        )
        stage = "open_orders_safety_check"
        existing_orders = await exchange.open_orders(settings.symbol)
        if existing_orders:
            raise RuntimeError(
                f"{settings.symbol} already has {len(existing_orders)} open Binance order(s). "
                "Cancel/resolve them before running the isolated protection validation."
            )

        stage = "ticker_price"
        price = await exchange.ticker_price(settings.symbol)
        stage = "quote_balance"
        quote_balance = await exchange.asset_balance(settings.quote_asset)
        reserve = quote_amount * 1.02
        if quote_balance < reserve:
            raise RuntimeError(
                f"Insufficient Testnet {settings.quote_asset}: need about {reserve:.8g}, "
                f"available {quote_balance:.8g}"
            )

        requested_qty = quote_amount / price
        qty = await exchange.normalize_quantity(settings.symbol, requested_qty, price)
        if qty <= 0:
            raise RuntimeError(
                "Requested validation amount is below Binance quantity/notional limits. "
                "Increase --quote-amount."
            )

        broker = Broker(settings, exchange, db)
        stage = "market_buy_and_oco"
        entry = await broker.enter(
            StrategySignal(SignalSide.BUY, 1.0, "Controlled Testnet protection validation"),
            RiskDecision(True, "Controlled validation quantity", quantity=qty),
            price,
        )
        if not entry.success:
            raise RuntimeError(f"Validation BUY failed: {entry.message}")

        trade = db.get_open_trade(settings.symbol, "testnet")
        if not trade or not trade.get("protective_list_client_order_id"):
            raise RuntimeError("BUY succeeded but no exchange protection was persisted")

        stage = "oco_verification"
        order_list = await exchange.get_order_list(
            settings.symbol,
            list_client_order_id=str(trade["protective_list_client_order_id"]),
        )
        if str(order_list.get("listOrderStatus") or "") not in {"EXECUTING", "ALL_DONE"}:
            raise RuntimeError(
                f"Unexpected OCO status after entry: {order_list.get('listOrderStatus')}"
            )

        # Simulate a restart by closing the first client and constructing a new
        # broker against the same persisted validation database.
        stage = "restart_simulation"
        await exchange.close()
        exchange = None
        restarted_exchange = BinanceClient(settings)
        restarted_broker = Broker(settings, restarted_exchange, db)

        stage = "restart_reconciliation"
        restart_price = await restarted_exchange.ticker_price(settings.symbol)
        restart_check = await restarted_broker.maybe_exit(
            StrategySignal(SignalSide.HOLD, 0.0, "Restart protection verification"),
            restart_price,
        )

        post_restart_trade = db.get_open_trade(settings.symbol, "testnet")
        restart_reconciled = (
            post_restart_trade is None
            or bool(post_restart_trade.get("protective_list_client_order_id"))
        )
        if not restart_reconciled:
            raise RuntimeError("Restart did not recover exchange protection state")

        if post_restart_trade is not None:
            stage = "cleanup_sell"
            cleanup_price = await restarted_exchange.ticker_price(settings.symbol)
            cleanup = await restarted_broker.maybe_exit(
                StrategySignal(SignalSide.SELL, 1.0, "Validation cleanup"),
                cleanup_price,
            )
            if not cleanup.success:
                raise RuntimeError(f"Validation cleanup SELL failed: {cleanup.message}")
        else:
            cleanup = restart_check

        remaining = db.get_open_trade(settings.symbol, "testnet")
        if remaining is not None:
            raise RuntimeError("Validation finished but the local test position is still open")

        return {
            "ok": True,
            "symbol": settings.symbol,
            "quote_amount_requested": quote_amount,
            "entry": {
                "message": entry.message,
                "details": entry.details,
            },
            "restart_check": {
                "action": restart_check.action,
                "message": restart_check.message,
            },
            "cleanup": {
                "action": cleanup.action,
                "message": cleanup.message,
            },
            "database": settings.database_path,
        }
    finally:
        if exchange is not None:
            await exchange.close()
        if restarted_exchange is not None:
            await restarted_exchange.close()
        process_lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Place a small real Binance Spot Testnet BUY, verify persisted OCO "
            "protection across a simulated restart, then close the test position."
        )
    )
    parser.add_argument(
        "--quote-amount",
        type=float,
        default=25.0,
        help="Approximate Testnet quote amount to use for the validation trade (default: 25)",
    )
    parser.add_argument(
        "--database",
        default="data/testnet_protection_validation.db",
        help="Isolated validation database path",
    )
    args = parser.parse_args()

    try:
        result = asyncio.run(run_validation(args.quote_amount, args.database))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "hint": (
                        "The validator stops before continuing whenever Binance "
                        "rejects a safety, entry, protection, or cleanup request."
                    ),
                },
                indent=2,
            )
        )
        return 1

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
