import argparse
import asyncio
import json
import sys
import uuid
from decimal import Decimal

from app.config import Settings
from app.exchange.binance import BinanceClient
from app.runtime_lock import RuntimeFileLock
from app.storage.db import TradingDB


async def build_cleanup_plan(settings: Settings, db: TradingDB) -> tuple[list[dict], list[BinanceClient]]:
    if settings.mode != "testnet":
        raise RuntimeError("Refusing cleanup: MODE must be testnet")
    if settings.allow_live_trading:
        raise RuntimeError("Refusing cleanup: ALLOW_LIVE_TRADING must be false")
    if settings.live_protection_validated:
        raise RuntimeError("Refusing cleanup: LIVE_PROTECTION_VALIDATED must be false")
    if settings.allow_multi_symbol_live:
        raise RuntimeError("Refusing cleanup: ALLOW_MULTI_SYMBOL_LIVE must be false")

    for symbol in settings.trading_symbols:
        if db.get_open_trade(symbol, settings.mode) is not None:
            raise RuntimeError(f"Refusing cleanup: local open position exists for {symbol}")
        unresolved = db.unresolved_orders(mode=settings.mode, symbol=symbol)
        if unresolved:
            ids = ", ".join(str(row["client_order_id"]) for row in unresolved)
            raise RuntimeError(f"Refusing cleanup: unresolved local order(s) exist for {symbol}: {ids}")

    clients: list[BinanceClient] = []
    primary = BinanceClient(settings)
    clients.append(primary)

    open_orders = await primary.open_orders()
    configured = set(settings.trading_symbols)
    blocking = [order for order in open_orders if str(order.get("symbol") or "") in configured]
    if blocking:
        compact = [{
            "symbol": item.get("symbol"),
            "orderId": item.get("orderId"),
            "clientOrderId": item.get("clientOrderId"),
            "side": item.get("side"),
            "type": item.get("type"),
        } for item in blocking]
        raise RuntimeError("Refusing cleanup: configured-symbol Binance open orders exist: " + json.dumps(compact))

    plan: list[dict] = []
    for symbol in settings.trading_symbols:
        base_asset = settings.base_for_symbol(symbol)
        if not base_asset:
            raise RuntimeError(f"Could not derive base asset for {symbol}")

        child_settings = settings.model_copy(update={
            "symbol": symbol,
            "base_asset": base_asset,
            "symbols": symbol,
        })
        client = primary if symbol == settings.symbol else BinanceClient(child_settings)
        if client is not primary:
            clients.append(client)

        balance = await client.asset_balance_details(base_asset)
        free = Decimal(str(balance["free"]))
        locked = Decimal(str(balance["locked"]))
        total = Decimal(str(balance["total"]))

        if locked > 0:
            raise RuntimeError(f"Refusing cleanup: {base_asset} has locked balance {locked}. Resolve exchange orders/locks first.")
        if free <= 0:
            plan.append({
                "symbol": symbol, "asset": base_asset, "available": float(total),
                "sell_quantity": 0.0, "estimated_quote": 0.0,
                "status": "SKIP_ZERO_BALANCE", "client": client,
            })
            continue

        price = Decimal(str(await client.ticker_price(symbol)))
        normalized = await client.normalize_quantity_decimal(symbol, free, price)
        if normalized <= 0:
            plan.append({
                "symbol": symbol, "asset": base_asset, "available": float(total),
                "sell_quantity": 0.0, "estimated_quote": 0.0,
                "status": "SKIP_DUST_OR_FILTER_LIMIT", "client": client,
            })
            continue

        plan.append({
            "symbol": symbol,
            "asset": base_asset,
            "available": float(total),
            "sell_quantity": float(normalized),
            "estimated_quote": float(normalized * price),
            "status": "READY",
            "client": client,
        })

    return plan, clients


async def execute_cleanup(plan: list[dict]) -> list[dict]:
    results: list[dict] = []
    for item in plan:
        if item["status"] != "READY":
            results.append({key: value for key, value in item.items() if key != "client"})
            continue

        client: BinanceClient = item["client"]
        symbol = str(item["symbol"])
        qty = float(item["sell_quantity"])
        client_order_id = f"agt-clean-{uuid.uuid4().hex[:20]}"

        try:
            order = await client.market_order(symbol, "SELL", qty, client_order_id=client_order_id)
        except Exception as submit_error:
            try:
                order = await client.get_order(symbol, client_order_id=client_order_id)
            except Exception as reconcile_error:
                raise RuntimeError(
                    f"{symbol} cleanup SELL outcome is uncertain. Do not rerun blindly. "
                    f"Check Binance Testnet order history first. Submit error: {submit_error}; "
                    f"reconcile error: {reconcile_error}"
                ) from submit_error

        if (
            order.get("symbol") != symbol
            or order.get("side") != "SELL"
            or order.get("clientOrderId") != client_order_id
        ):
            raise RuntimeError(f"{symbol} cleanup order identity mismatch; stop and inspect Testnet")

        executed = client.executed_quantity(order)
        fill_price = client.weighted_fill_price(order, 0.0) if executed > 0 else 0.0
        results.append({
            "symbol": symbol,
            "asset": item["asset"],
            "requested_quantity": qty,
            "executed_quantity": executed,
            "average_fill_price": fill_price,
            "status": str(order.get("status") or "UNKNOWN"),
            "order_id": order.get("orderId"),
            "client_order_id": client_order_id,
        })

        if str(order.get("status") or "") not in {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}:
            raise RuntimeError(f"{symbol} cleanup order is not terminal ({order.get('status')}). Stop and inspect Testnet before continuing.")

    return results


def printable_plan(plan: list[dict]) -> list[dict]:
    return [{key: value for key, value in item.items() if key != "client"} for item in plan]


async def async_main(auto_yes: bool) -> int:
    settings = Settings()
    db = TradingDB(settings.database_path)
    db.init()
    lock = RuntimeFileLock(settings.database_path)
    lock.acquire()

    clients: list[BinanceClient] = []
    try:
        plan, clients = await build_cleanup_plan(settings, db)
        print(json.dumps({
            "mode": settings.mode,
            "action": "TESTNET_BALANCE_CLEANUP",
            "plan": printable_plan(plan),
            "note": (
                "This utility only sells configured Testnet base-asset balances "
                f"({', '.join(settings.trading_symbols)}) to {settings.quote_asset}."
            ),
        }, indent=2))

        ready = [item for item in plan if item["status"] == "READY"]
        if not ready:
            print("\nNothing sellable was found. Only zero/dust balances remain.")
            return 0

        if not auto_yes:
            answer = input("\nType SELL TESTNET to execute these Testnet market SELL orders: ").strip()
            if answer != "SELL TESTNET":
                print("Cleanup canceled. No orders were submitted.")
                return 2

        results = await execute_cleanup(plan)
        print("\nCleanup result:")
        print(json.dumps(results, indent=2))
        print("\nRun python check_live_readiness.py, then start the bot and check /status to confirm unmanaged exposure is below the configured limit.")
        return 0
    finally:
        for client in clients:
            await client.close()
        lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely sell configured Binance Spot Testnet base-asset balances back to the configured quote asset."
    )
    parser.add_argument("--yes", action="store_true", help="Skip the interactive SELL TESTNET confirmation.")
    args = parser.parse_args()
    try:
        return asyncio.run(async_main(args.yes))
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "note": "Cleanup stopped fail-closed. Do not rerun blindly after an uncertain order outcome.",
        }, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
