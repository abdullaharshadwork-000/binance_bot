import argparse
import asyncio
import json
import math
import sys
from decimal import Decimal, InvalidOperation

from app.config import Settings
from app.exchange.binance import BinanceClient
from app.execution.broker import Broker
from app.storage.db import TradingDB


def _item(name: str, ok: bool, detail: str) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail}


def _unconfigured_account_assets(account: dict, settings: Settings, symbols: list[str]):
    balances = account.get("balances") if isinstance(account, dict) else None
    if not isinstance(balances, list):
        return [], "Signed account response omitted its balances list"
    configured_assets = {
        settings.base_for_symbol(symbol) for symbol in symbols
    }
    configured_assets.discard(None)
    configured_assets.add(settings.quote_asset)
    totals: dict[str, Decimal] = {}
    for row in balances:
        if not isinstance(row, dict):
            return [], "Signed account response contains an invalid balance row"
        asset = str(row.get("asset") or "").strip().upper()
        if not asset:
            return [], "Signed account balance row is missing its asset identifier"
        if asset in configured_assets:
            continue
        try:
            total = Decimal(str(row.get("free") or "0")) + Decimal(str(row.get("locked") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            return [], f"Could not parse the {asset} account balance"
        if not total.is_finite() or total < 0:
            return [], f"Invalid free/locked quantity for account asset {asset}"
        if total > 0:
            totals[asset] = totals.get(asset, Decimal("0")) + total
    return [{"asset": asset, "quantity": float(amount)} for asset, amount in sorted(totals.items())], None


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

    saved_universe = db.saved_trading_universe(mode=settings.mode)
    symbols = list(dict.fromkeys([
        *settings.trading_symbols,
        *settings.configured_symbols,
        *saved_universe,
        *db.tracked_symbols(mode=settings.mode),
    ]))
    if settings.universe_mode == "top_volume":
        checks.append(
            _item(
                "dynamic_universe_recorded",
                bool(saved_universe),
                (", ".join(saved_universe) if saved_universe else "Refresh the dashboard universe before readiness review"),
            )
        )

    unresolved_total = 0
    for symbol in symbols:
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
    protection_inspector = (
        Broker(settings, exchange, db)
        if settings.mode in {"testnet", "live"}
        else None
    )
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

        other_assets, balance_error = _unconfigured_account_assets(
            account, settings, symbols
        )
        if balance_error:
            checks.append(_item("other_account_assets_valued", False, balance_error))
        elif not other_assets:
            checks.append(
                _item(
                    "other_account_assets_valued",
                    True,
                    "no non-quote balances outside the selected universe",
                )
            )
        else:
            try:
                pairs = await exchange.asset_quote_conversion_pairs(
                    [item["asset"] for item in other_assets], settings.quote_asset
                )
                missing_assets = [
                    item["asset"] for item in other_assets if item["asset"] not in pairs
                ]
                if missing_assets:
                    raise RuntimeError(
                        "No active direct/inverse Spot quote pair for: "
                        + ", ".join(missing_assets)
                    )
                prices = await exchange.ticker_prices(
                    list(dict.fromkeys(item["symbol"] for item in pairs.values()))
                )
                valuations = []
                for item in other_assets:
                    pair = pairs[item["asset"]]
                    price = prices.get(pair["symbol"])
                    if price is None or not math.isfinite(price) or price <= 0:
                        raise RuntimeError(
                            f"Fresh {settings.quote_asset} price unavailable for {item['asset']}"
                        )
                    value = (
                        item["quantity"] / price
                        if pair["inverse"]
                        else item["quantity"] * price
                    )
                    if not math.isfinite(value) or value < 0:
                        raise RuntimeError(
                            f"Invalid quote valuation for {item['asset']}"
                        )
                    valuations.append(
                        f"{item['asset']}={value:.8g} {settings.quote_asset}"
                    )
                checks.append(
                    _item(
                        "other_account_assets_valued",
                        True,
                        "; ".join(valuations),
                    )
                )
            except Exception as exc:
                checks.append(_item("other_account_assets_valued", False, str(exc)))

        open_orders = await exchange.open_orders()
        configured = set(symbols)
        configured_open = [
            order for order in open_orders if order.get("symbol") in configured
        ]

        for symbol in symbols:
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

            if protection_inspector is None:
                checks.append(
                    _item(
                        f"{symbol}_exchange_protection_query",
                        False,
                        "exchange OCO inspection is only available in Testnet/live mode",
                    )
                )
                continue

            try:
                protection = await protection_inspector.inspect_exchange_protection(trade)
            except Exception as exc:
                checks.append(
                    _item(
                        f"{symbol}_exchange_protection_query",
                        False,
                        str(exc),
                    )
                )
                continue

            status = protection["status"]
            checks.append(
                _item(
                    f"{symbol}_exchange_protection_query",
                    True,
                    "queried the stored OCO and both Binance child orders",
                )
            )
            checks.append(
                _item(
                    f"{symbol}_exchange_protection_identity",
                    True,
                    f"status={status}, orderListId={protection['order_list_id']}",
                )
            )
            checks.append(
                _item(
                    f"{symbol}_exchange_protection_children_verified",
                    protection["verified"],
                    json.dumps(protection["children"]),
                )
            )
            checks.append(
                _item(
                    f"{symbol}_exchange_protection_both_legs_active",
                    protection["protected"],
                    f"status={status}; protected={protection['protected']}",
                )
            )
            checks.append(
                _item(
                    f"{symbol}_open_orders_visible",
                    protection["protected"] and len(symbol_orders) >= 2,
                    f"{len(symbol_orders)} open exchange order(s) for symbol; expected both OCO legs",
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
            "configured_symbols": symbols,
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
