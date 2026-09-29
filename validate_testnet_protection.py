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


def build_validation_settings(
    base: Settings,
    database_path: str,
    *,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> Settings:
    effective_stop = base.stop_loss_pct if stop_loss_pct is None else stop_loss_pct
    effective_take = base.take_profit_pct if take_profit_pct is None else take_profit_pct
    trailing_distance = min(
        base.trailing_stop_distance_pct,
        max(effective_take / 2, 0.00001),
    )
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
        stop_loss_pct=effective_stop,
        take_profit_pct=effective_take,
        enable_trailing_stop=base.enable_trailing_stop,
        trailing_stop_activation_pct=base.trailing_stop_activation_pct,
        trailing_stop_distance_pct=trailing_distance,
        breakeven_activation_pct=base.breakeven_activation_pct,
        trading_fee_bps=base.trading_fee_bps,
        max_entry_spread_bps=base.max_entry_spread_bps,
        max_entry_slippage_bps=base.max_entry_slippage_bps,
        account_refresh_seconds=base.account_refresh_seconds,
        account_max_age_seconds=base.account_max_age_seconds,
    )


async def open_offline_fill_validation(
    quote_amount: float,
    database_path: str,
    target: str,
    trigger_pct: float,
) -> dict:
    base = Settings()
    if base.mode != "testnet":
        raise RuntimeError("Offline-fill validation requires MODE=testnet in .env")
    if quote_amount <= 0:
        raise ValueError("--quote-amount must be positive")
    if target not in {"tp", "sl"}:
        raise ValueError("--target must be either tp or sl")
    if not (0 < trigger_pct <= 0.05):
        raise ValueError("--trigger-pct must be greater than 0 and at most 0.05")

    process_lock = RuntimeFileLock(base.database_path)
    process_lock.acquire()
    exchange: BinanceClient | None = None
    try:
        if target == "tp":
            stop_loss_pct = max(base.stop_loss_pct, trigger_pct * 4)
            take_profit_pct = trigger_pct
        else:
            stop_loss_pct = trigger_pct
            take_profit_pct = max(base.take_profit_pct, trigger_pct * 4)

        settings = build_validation_settings(
            base,
            database_path,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
        settings.ensure_directories()
        db = TradingDB(settings.database_path)
        db.init()

        if db.get_open_trade(settings.symbol, "testnet") is not None:
            raise RuntimeError(
                f"Validation database already has an open {settings.symbol} position; "
                "use --resume before starting an offline-fill test"
            )

        exchange = BinanceClient(settings)
        await exchange.ping()
        await exchange.validate_symbol_assets(
            settings.symbol,
            settings.base_asset,
            settings.quote_asset,
        )
        existing_orders = await exchange.open_orders(settings.symbol)
        if existing_orders:
            raise RuntimeError(
                f"{settings.symbol} already has {len(existing_orders)} open Binance order(s). "
                "Resolve them before starting the offline-fill test."
            )

        price = await exchange.ticker_price(settings.symbol)
        quote_balance = await exchange.asset_balance(settings.quote_asset)
        reserve = quote_amount * 1.02
        if quote_balance < reserve:
            raise RuntimeError(
                f"Insufficient Testnet {settings.quote_asset}: need about {reserve:.8g}, "
                f"available {quote_balance:.8g}"
            )

        qty = await exchange.normalize_quantity(
            settings.symbol,
            quote_amount / price,
            price,
        )
        if qty <= 0:
            raise RuntimeError(
                "Requested validation amount is below Binance quantity/notional limits. "
                "Increase --quote-amount."
            )

        broker = Broker(settings, exchange, db)
        entry = await broker.enter(
            StrategySignal(
                SignalSide.BUY,
                1.0,
                f"Offline Binance {target.upper()} execution validation",
            ),
            RiskDecision(True, "Controlled validation quantity", quantity=qty),
            price,
        )
        if not entry.success:
            raise RuntimeError(f"Offline-fill BUY failed: {entry.message}")

        trade = db.get_open_trade(settings.symbol, "testnet")
        if trade is None or not trade.get("protective_list_client_order_id"):
            raise RuntimeError(
                "Offline-fill BUY succeeded but persisted OCO protection is missing"
            )

        order_list = await exchange.get_order_list(
            settings.symbol,
            list_client_order_id=str(trade["protective_list_client_order_id"]),
        )
        if str(order_list.get("listOrderStatus") or "") != "EXECUTING":
            raise RuntimeError(
                "Offline-fill test requires an active Binance OCO before Python exits"
            )

        return {
            "ok": True,
            "phase": "open_offline_fill",
            "target": target,
            "symbol": settings.symbol,
            "message": (
                "The Python process is ending with Binance OCO protection active. "
                "Leave the normal bot stopped. After Binance fills one OCO leg, run "
                "--recover-offline-fill."
            ),
            "trade_id": int(trade["id"]),
            "quantity": float(trade["quantity"]),
            "entry_price": float(trade["entry_price"]),
            "stop_price": float(trade["stop_price"]),
            "take_profit_price": float(trade["take_profit_price"]),
            "trigger_pct": trigger_pct,
            "protective_list_client_order_id": trade["protective_list_client_order_id"],
            "protective_order_list_id": trade["protective_order_list_id"],
            "protection_status": trade["protection_status"],
            "database": settings.database_path,
        }
    finally:
        if exchange is not None:
            await exchange.close()
        process_lock.release()


async def recover_offline_fill_validation(database_path: str) -> dict:
    base = Settings()
    if base.mode != "testnet":
        raise RuntimeError("Offline-fill recovery requires MODE=testnet in .env")

    process_lock = RuntimeFileLock(base.database_path)
    process_lock.acquire()
    exchange: BinanceClient | None = None
    try:
        settings = build_validation_settings(base, database_path)
        settings.ensure_directories()
        db = TradingDB(settings.database_path)
        db.init()

        trade = db.get_open_trade(settings.symbol, "testnet")
        if trade is None:
            raise RuntimeError(
                "No open validation position exists. The local ledger has nothing to recover."
            )

        list_client_id = trade.get("protective_list_client_order_id")
        order_list_id = trade.get("protective_order_list_id")
        if not list_client_id or order_list_id is None:
            raise RuntimeError(
                "Stored validation position has no persisted Binance OCO identity"
            )

        exchange = BinanceClient(settings)
        await exchange.ping()
        order_list = await exchange.get_order_list(
            settings.symbol,
            list_client_order_id=str(list_client_id),
        )
        if str(order_list.get("listClientOrderId") or "") != str(list_client_id):
            raise RuntimeError("Offline recovery found a different OCO client id")
        if str(order_list.get("orderListId")) != str(order_list_id):
            raise RuntimeError("Offline recovery found a different Binance order-list id")
        if str(order_list.get("symbol") or "") != settings.symbol:
            raise RuntimeError("Offline recovery OCO symbol mismatch")

        status_before = str(order_list.get("listOrderStatus") or "UNKNOWN")
        if status_before == "EXECUTING":
            raise RuntimeError(
                "Binance OCO is still EXECUTING; no protective leg has filled yet. "
                "Leave the normal bot stopped and rerun --recover-offline-fill after "
                "the Testnet price reaches the stop-loss or take-profit."
            )
        if status_before != "ALL_DONE":
            raise RuntimeError(
                f"Unexpected Binance OCO status for offline recovery: {status_before}"
            )

        with db.connection() as conn:
            sell_orders_before = int(
                conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE mode='testnet' AND symbol=? AND side='SELL'",
                    (settings.symbol,),
                ).fetchone()[0]
            )

        broker = Broker(settings, exchange, db)
        current_price = await exchange.ticker_price(settings.symbol)
        reconciliation = await broker.maybe_exit(
            StrategySignal(
                SignalSide.HOLD,
                0.0,
                "Offline Binance protection fill reconciliation",
            ),
            current_price,
        )

        if db.get_open_trade(settings.symbol, "testnet") is not None:
            raise RuntimeError(
                "Binance reported ALL_DONE but the broker did not close the local position"
            )
        if reconciliation.action != "SELL" or not reconciliation.success:
            raise RuntimeError(
                "Offline protective fill did not reconcile as a successful exchange SELL"
            )
        if "exchange" not in reconciliation.message.lower():
            raise RuntimeError(
                "Recovery closed the position without identifying an exchange protective fill"
            )

        with db.connection() as conn:
            sell_orders_after = int(
                conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE mode='testnet' AND symbol=? AND side='SELL'",
                    (settings.symbol,),
                ).fetchone()[0]
            )
        if sell_orders_after != sell_orders_before:
            raise RuntimeError(
                "Recovery recorded a new bot market SELL; offline OCO recovery must "
                "use only the exchange protective fill"
            )

        return {
            "ok": True,
            "phase": "recover_offline_fill",
            "symbol": settings.symbol,
            "same_oco_verified": True,
            "protective_list_client_order_id": list_client_id,
            "protective_order_list_id": order_list_id,
            "oco_status_before_reconciliation": status_before,
            "reconciliation": {
                "action": reconciliation.action,
                "message": reconciliation.message,
                "details": reconciliation.details,
            },
            "new_bot_sell_orders_during_recovery": 0,
            "database": settings.database_path,
        }
    finally:
        if exchange is not None:
            await exchange.close()
        process_lock.release()


async def open_and_exit_validation(quote_amount: float, database_path: str) -> dict:
    base = Settings()
    if base.mode != "testnet":
        raise RuntimeError("Process-restart validation requires MODE=testnet in .env")
    if quote_amount <= 0:
        raise ValueError("--quote-amount must be positive")

    process_lock = RuntimeFileLock(base.database_path)
    process_lock.acquire()
    exchange: BinanceClient | None = None
    try:
        settings = build_validation_settings(base, database_path)
        settings.ensure_directories()
        db = TradingDB(settings.database_path)
        db.init()

        if db.get_open_trade(settings.symbol, "testnet") is not None:
            raise RuntimeError(
                f"Validation database already has an open {settings.symbol} position; "
                "use --recover-and-close before starting another restart test"
            )

        exchange = BinanceClient(settings)
        await exchange.ping()
        await exchange.validate_symbol_assets(
            settings.symbol,
            settings.base_asset,
            settings.quote_asset,
        )

        existing_orders = await exchange.open_orders(settings.symbol)
        if existing_orders:
            raise RuntimeError(
                f"{settings.symbol} already has {len(existing_orders)} open Binance order(s). "
                "Resolve them before starting the isolated restart test."
            )

        price = await exchange.ticker_price(settings.symbol)
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
        entry = await broker.enter(
            StrategySignal(
                SignalSide.BUY,
                1.0,
                "Two-process Testnet restart validation",
            ),
            RiskDecision(True, "Controlled validation quantity", quantity=qty),
            price,
        )
        if not entry.success:
            raise RuntimeError(f"Restart-test BUY failed: {entry.message}")

        trade = db.get_open_trade(settings.symbol, "testnet")
        if trade is None or not trade.get("protective_list_client_order_id"):
            raise RuntimeError(
                "Restart-test BUY succeeded but persisted OCO protection is missing"
            )

        order_list = await exchange.get_order_list(
            settings.symbol,
            list_client_order_id=str(trade["protective_list_client_order_id"]),
        )
        if str(order_list.get("listClientOrderId") or "") != str(
            trade["protective_list_client_order_id"]
        ):
            raise RuntimeError("Binance returned a different OCO client id")
        if str(order_list.get("orderListId")) != str(
            trade.get("protective_order_list_id")
        ):
            raise RuntimeError("Binance returned a different OCO order-list id")
        if str(order_list.get("listOrderStatus") or "") != "EXECUTING":
            raise RuntimeError(
                "Restart test requires an actively protected position before process exit; "
                f"Binance reported {order_list.get('listOrderStatus')}"
            )

        return {
            "ok": True,
            "phase": "open_and_exit",
            "symbol": settings.symbol,
            "message": (
                "Testnet position and OCO are active. This command is ending now. "
                "Do not start the normal bot; run --recover-and-close in a new process."
            ),
            "trade_id": int(trade["id"]),
            "quantity": float(trade["quantity"]),
            "entry_price": float(trade["entry_price"]),
            "stop_price": float(trade["stop_price"]),
            "take_profit_price": float(trade["take_profit_price"]),
            "protective_list_client_order_id": trade["protective_list_client_order_id"],
            "protective_order_list_id": trade["protective_order_list_id"],
            "protection_status": trade["protection_status"],
            "database": settings.database_path,
        }
    finally:
        if exchange is not None:
            await exchange.close()
        process_lock.release()


async def recover_and_close_validation(database_path: str) -> dict:
    base = Settings()
    if base.mode != "testnet":
        raise RuntimeError("Process-restart recovery requires MODE=testnet in .env")

    process_lock = RuntimeFileLock(base.database_path)
    process_lock.acquire()
    exchange: BinanceClient | None = None
    try:
        settings = build_validation_settings(base, database_path)
        settings.ensure_directories()
        db = TradingDB(settings.database_path)
        db.init()

        trade = db.get_open_trade(settings.symbol, "testnet")
        if trade is None:
            raise RuntimeError(
                "No open validation position exists. Run --open-and-exit first."
            )

        list_client_id = trade.get("protective_list_client_order_id")
        order_list_id = trade.get("protective_order_list_id")
        if not list_client_id or order_list_id is None:
            raise RuntimeError(
                "Stored validation position has no persisted Binance OCO identity; "
                "use --resume for conservative recovery instead"
            )

        exchange = BinanceClient(settings)
        await exchange.ping()
        await exchange.validate_symbol_assets(
            settings.symbol,
            settings.base_asset,
            settings.quote_asset,
        )

        # The critical assertion for this phase: a fresh Python process must
        # rediscover the exact OCO created by the previous process. Do not
        # recreate missing protection here; fail closed if it cannot be found.
        order_list = await exchange.get_order_list(
            settings.symbol,
            list_client_order_id=str(list_client_id),
        )
        if str(order_list.get("listClientOrderId") or "") != str(list_client_id):
            raise RuntimeError("Restart recovery found a different OCO client id")
        if str(order_list.get("orderListId")) != str(order_list_id):
            raise RuntimeError("Restart recovery found a different Binance order-list id")
        if str(order_list.get("symbol") or "") != settings.symbol:
            raise RuntimeError("Restart recovery OCO symbol mismatch")

        status_before = str(order_list.get("listOrderStatus") or "UNKNOWN")
        if status_before not in {"EXECUTING", "ALL_DONE"}:
            raise RuntimeError(
                f"Unexpected persisted OCO status after real process restart: {status_before}"
            )

        broker = Broker(settings, exchange, db)
        current_price = await exchange.ticker_price(settings.symbol)
        reconciliation = await broker.maybe_exit(
            StrategySignal(
                SignalSide.HOLD,
                0.0,
                "Real process restart reconciliation",
            ),
            current_price,
        )

        trade_after_reconcile = db.get_open_trade(settings.symbol, "testnet")
        if trade_after_reconcile is None:
            return {
                "ok": True,
                "phase": "recover_and_close",
                "symbol": settings.symbol,
                "same_oco_verified": True,
                "oco_status_before_reconciliation": status_before,
                "message": "Exchange protection closed the position while the bot was stopped.",
                "reconciliation": {
                    "action": reconciliation.action,
                    "message": reconciliation.message,
                },
                "database": settings.database_path,
            }

        cleanup_price = await exchange.ticker_price(settings.symbol)
        cleanup = await broker.maybe_exit(
            StrategySignal(
                SignalSide.SELL,
                1.0,
                "Real process restart validation cleanup",
            ),
            cleanup_price,
        )
        if not cleanup.success:
            raise RuntimeError(
                f"Restart validation cleanup SELL failed: {cleanup.message}"
            )

        if db.get_open_trade(settings.symbol, "testnet") is not None:
            raise RuntimeError(
                "Restart validation cleanup returned success but position is still open"
            )

        return {
            "ok": True,
            "phase": "recover_and_close",
            "symbol": settings.symbol,
            "same_oco_verified": True,
            "protective_list_client_order_id": list_client_id,
            "protective_order_list_id": order_list_id,
            "oco_status_before_reconciliation": status_before,
            "reconciliation": {
                "action": reconciliation.action,
                "message": reconciliation.message,
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
        process_lock.release()


async def resume_validation(database_path: str) -> dict:
    base = Settings()
    if base.mode != "testnet":
        raise RuntimeError("Validation recovery requires MODE=testnet in .env")

    process_lock = RuntimeFileLock(base.database_path)
    process_lock.acquire()
    exchange: BinanceClient | None = None
    try:
        settings = build_validation_settings(base, database_path)
        settings.ensure_directories()
        db = TradingDB(settings.database_path)
        db.init()
        trade = db.get_open_trade(settings.symbol, "testnet")
        if trade is None:
            return {
                "ok": True,
                "recovered": True,
                "symbol": settings.symbol,
                "message": "No open validation position remains in the validation database.",
                "database": settings.database_path,
            }

        exchange = BinanceClient(settings)
        await exchange.ping()
        await exchange.validate_symbol_assets(
            settings.symbol,
            settings.base_asset,
            settings.quote_asset,
        )

        broker = Broker(settings, exchange, db)
        current_price = await exchange.ticker_price(settings.symbol)

        # First reconcile any persisted OCO or install protection if the position
        # is still open and safely inside its original hard exit levels.
        reconciliation = await broker.maybe_exit(
            StrategySignal(
                SignalSide.HOLD,
                0.0,
                "Validation recovery reconciliation",
            ),
            current_price,
        )

        trade = db.get_open_trade(settings.symbol, "testnet")
        if trade is None:
            return {
                "ok": True,
                "recovered": True,
                "symbol": settings.symbol,
                "message": "Validation position was already closed by exchange protection.",
                "reconciliation": {
                    "action": reconciliation.action,
                    "message": reconciliation.message,
                },
                "database": settings.database_path,
            }

        # The local position still exists. Close only this recorded validation
        # position through the normal broker path, which reconciles/cancels its
        # OCO before submitting the Testnet market SELL.
        cleanup_price = await exchange.ticker_price(settings.symbol)
        cleanup = await broker.maybe_exit(
            StrategySignal(
                SignalSide.SELL,
                1.0,
                "Validation recovery cleanup",
            ),
            cleanup_price,
        )
        if not cleanup.success:
            raise RuntimeError(
                f"Validation recovery cleanup failed: {cleanup.message}"
            )

        if db.get_open_trade(settings.symbol, "testnet") is not None:
            raise RuntimeError(
                "Validation recovery finished but the local test position is still open"
            )

        return {
            "ok": True,
            "recovered": True,
            "symbol": settings.symbol,
            "reconciliation": {
                "action": reconciliation.action,
                "message": reconciliation.message,
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
        process_lock.release()


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
        try:
            existing_orders = await exchange.open_orders(settings.symbol)
        except Exception as exc:
            raise RuntimeError(
                f"{stage}: Binance rejected the account open-order safety query: {exc}"
            ) from exc
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
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Safely reconcile and close an existing validation position from "
            "the isolated validation database instead of opening a new one"
        ),
    )
    parser.add_argument(
        "--open-and-exit",
        action="store_true",
        help=(
            "Open a small real Testnet position, verify its OCO, then end this "
            "Python process while leaving the position protected on Binance"
        ),
    )
    parser.add_argument(
        "--recover-and-close",
        action="store_true",
        help=(
            "In a new Python process, verify the exact persisted OCO survived "
            "the restart, reconcile it, and close the validation position"
        ),
    )
    parser.add_argument(
        "--open-offline-fill",
        action="store_true",
        help=(
            "Open a Testnet position with a close OCO trigger, then exit Python "
            "so Binance can close it while the bot is offline"
        ),
    )
    parser.add_argument(
        "--recover-offline-fill",
        action="store_true",
        help=(
            "Recover only after Binance has filled an OCO leg while Python was stopped; "
            "fails if the OCO is still active"
        ),
    )
    parser.add_argument(
        "--target",
        choices=["tp", "sl"],
        default="tp",
        help="Offline-fill leg to make closer to entry (tp or sl; default: tp)",
    )
    parser.add_argument(
        "--trigger-pct",
        type=float,
        default=0.003,
        help="Distance from entry for the target protective trigger (default: 0.003 = 0.3%%)",
    )
    args = parser.parse_args()
    selected_modes = sum(
        bool(value)
        for value in (
            args.resume,
            args.open_and_exit,
            args.recover_and_close,
            args.open_offline_fill,
            args.recover_offline_fill,
        )
    )
    if selected_modes > 1:
        parser.error(
            "Choose only one validation mode at a time"
        )

    try:
        if args.resume:
            result = asyncio.run(resume_validation(args.database))
        elif args.open_and_exit:
            result = asyncio.run(
                open_and_exit_validation(args.quote_amount, args.database)
            )
        elif args.recover_and_close:
            result = asyncio.run(
                recover_and_close_validation(args.database)
            )
        elif args.open_offline_fill:
            result = asyncio.run(
                open_offline_fill_validation(
                    args.quote_amount,
                    args.database,
                    args.target,
                    args.trigger_pct,
                )
            )
        elif args.recover_offline_fill:
            result = asyncio.run(
                recover_offline_fill_validation(args.database)
            )
        else:
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
