import argparse
import asyncio
import json
import sys

from app.config import Settings
from app.exchange.binance import BinanceClient
from app.storage.db import TradingDB


def _item(name: str, ok: bool, detail: str) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail}


async def run_readiness_check() -> dict:
    settings = Settings()
    db = TradingDB(settings.database_path)
    db.init()
    checks: list[dict] = []

    checks.append(
        _item(
            "mode_is_testnet",
            settings.mode == "testnet",
            f"MODE={settings.mode}",
        )
    )
    checks.append(
        _item(
            "live_execution_disabled",
            not settings.allow_live_trading,
            f"ALLOW_LIVE_TRADING={str(settings.allow_live_trading).lower()}",
        )
    )
    checks.append(
        _item(
            "live_validation_gate_still_closed",
            not settings.live_protection_validated,
            f"LIVE_PROTECTION_VALIDATED={str(settings.live_protection_validated).lower()}",
        )
    )
    checks.append(
        _item(
            "multi_symbol_live_disabled",
            not settings.allow_multi_symbol_live,
            f"ALLOW_MULTI_SYMBOL_LIVE={str(settings.allow_multi_symbol_live).lower()}",
        )
    )

    unresolved_total = 0
    for symbol in settings.trading_symbols:
        unresolved = db.unresolved_orders(mode=settings.mode, symbol=symbol)
        unresolved_total += len(unresolved)
        checks.append(
            _item(
                f"{symbol}_no_unresolved_local_orders",
                not unresolved,
                (
                    "none"
                    if not unresolved
                    else ", ".join(
                        f"{row['client_order_id']}:{row['status']}" for row in unresolved
                    )
                ),
            )
        )

    exchange = BinanceClient(settings)
    try:
        await exchange.ping()
        checks.append(_item("binance_api_reachable", True, "ping succeeded"))

        account = await exchange.account()
        checks.append(
            _item(
                "signed_account_api_reachable",
                isinstance(account.get("balances"), list),
                "signed account query succeeded",
            )
        )

        open_orders = await exchange.open_orders()
        configured = set(settings.trading_symbols)
        configured_open = [
            order for order in open_orders if order.get("symbol") in configured
        ]

        for symbol in settings.trading_symbols:
            trade = db.get_open_trade(symbol, settings.mode)
            symbol_orders = [
                order for order in configured_open if order.get("symbol") == symbol
            ]
            if trade is None:
                bot_owned_orders = [
                    order
                    for order in symbol_orders
                    if str(order.get("clientOrderId") or "").startswith("agt-")
                ]
                checks.append(
                    _item(
                        f"{symbol}_local_position_state",
                        True,
                        "no local open position",
                    )
                )
                checks.append(
                    _item(
                        f"{symbol}_no_orphan_bot_orders",
                        not bot_owned_orders,
                        (
                            "none"
                            if not bot_owned_orders
                            else json.dumps([
                                {
                                    "orderId": order.get("orderId"),
                                    "clientOrderId": order.get("clientOrderId"),
                                    "side": order.get("side"),
                                    "type": order.get("type"),
                                }
                                for order in bot_owned_orders
                            ])
                        ),
                    )
                )
                continue

            list_client_id = trade.get("protective_list_client_order_id")
            checks.append(
                _item(
                    f"{symbol}_open_position_has_protection_identity",
                    bool(list_client_id),
                    (
                        f"listClientOrderId={list_client_id}"
                        if list_client_id
                        else "local position has no persisted OCO list client id"
                    ),
                )
            )
            if not list_client_id:
                continue

            try:
                order_list = await exchange.get_order_list(
                    symbol,
                    list_client_order_id=str(list_client_id),
                )
            except Exception as exc:
                checks.append(
                    _item(
                        f"{symbol}_exchange_protection_query",
                        False,
                        str(exc),
                    )
                )
                continue

            identity_ok = (
                str(order_list.get("listClientOrderId") or "") == str(list_client_id)
                and str(order_list.get("symbol") or "") == symbol
            )
            status = str(
                order_list.get("listOrderStatus")
                or order_list.get("listStatusType")
                or "UNKNOWN"
            )
            checks.append(
                _item(
                    f"{symbol}_exchange_protection_identity",
                    identity_ok,
                    f"status={status}, orderListId={order_list.get('orderListId')}",
                )
            )
            checks.append(
                _item(
                    f"{symbol}_exchange_protection_active_or_terminal",
                    status in {"EXECUTING", "ALL_DONE"},
                    f"status={status}",
                )
            )
            checks.append(
                _item(
                    f"{symbol}_open_orders_visible",
                    bool(symbol_orders) or status == "ALL_DONE",
                    f"{len(symbol_orders)} open exchange order(s) for symbol",
                )
            )

        unexpected = [
            {
                "symbol": order.get("symbol"),
                "orderId": order.get("orderId"),
                "clientOrderId": order.get("clientOrderId"),
                "side": order.get("side"),
                "type": order.get("type"),
            }
            for order in open_orders
            if order.get("symbol") in configured
            and not str(order.get("clientOrderId") or "").startswith("agt-")
        ]
        checks.append(
            _item(
                "no_unexpected_configured_symbol_open_orders",
                not unexpected,
                "none" if not unexpected else json.dumps(unexpected),
            )
        )
    finally:
        await exchange.close()

    passed = sum(1 for item in checks if item["ok"])
    failed = len(checks) - passed
    return {
        "ok": failed == 0,
        "summary": {
            "passed": passed,
            "failed": failed,
            "total": len(checks),
            "configured_symbols": settings.trading_symbols,
            "local_unresolved_orders": unresolved_total,
        },
        "checks": checks,
        "note": (
            "This command is read-only. It does not place orders, change configuration, "
            "or declare the strategy profitable/live-ready."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Testnet/config/ledger checks before any live-mode decision."
    )
    parser.parse_args()
    try:
        result = asyncio.run(run_readiness_check())
    except Exception as exc:
        result = {
            "ok": False,
            "error": str(exc),
            "note": "No order was intentionally submitted by this readiness checker.",
        }
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
