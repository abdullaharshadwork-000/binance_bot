import json
import logging
import math
import time
import uuid
from decimal import Decimal

from app.config import Settings
from app.exchange.binance import BinanceClient
from app.models import ExecutionResult, RiskDecision, SignalSide, StrategySignal
from app.storage.db import TradingDB

logger = logging.getLogger(__name__)


class Broker:
    def __init__(self, settings: Settings, exchange: BinanceClient, db: TradingDB):
        self.settings = settings
        self.exchange = exchange
        self.db = db
        self._inventory_checked_at = 0.0
        self._inventory_issue: str | None = None
        self._init_paper_balances()

    @property
    def _paper_quote_key(self) -> str:
        # Quote cash is shared by symbols that trade the same quote asset, but it
        # must never leak between paper/testnet/live portfolios.
        return f"paper_balance:{self.settings.mode}:quote:{self.settings.quote_asset}"

    @property
    def _paper_base_key(self) -> str:
        # Base inventory is symbol-specific. A single global base balance made an
        # ETH bot value and sell BTC inventory in multi-symbol paper mode.
        return f"paper_balance:{self.settings.mode}:base:{self.settings.symbol}"

    def _init_paper_balances(self):
        if self.settings.mode != "paper":
            return

        # Preserve balances from the pre-namespacing schema once. The manager
        # constructs the primary symbol first, so any legacy base inventory is
        # assigned only to that symbol rather than copied into every market.
        migration_key = "paper_balance:namespaced_migration_owner"
        migration_owner = self.db.get_state(migration_key)
        if migration_owner is None:
            legacy_quote = self.db.get_state("paper_quote_balance")
            legacy_base = self.db.get_state("paper_base_balance")
            self.db.set_states({
                migration_key: self.settings.symbol,
                self._paper_quote_key: (
                    legacy_quote
                    if legacy_quote is not None
                    else self.settings.paper_starting_balance
                ),
                self._paper_base_key: legacy_base if legacy_base is not None else 0.0,
            })
            return

        if self.db.get_state(self._paper_quote_key) is None:
            self.db.set_state(self._paper_quote_key, self.settings.paper_starting_balance)
        if self.db.get_state(self._paper_base_key) is None:
            self.db.set_state(self._paper_base_key, 0.0)

    def _paper_quote_dec(self) -> Decimal:
        return Decimal(str(self.db.get_state(self._paper_quote_key, "0") or "0"))

    def _paper_base_dec(self) -> Decimal:
        return Decimal(str(self.db.get_state(self._paper_base_key, "0") or "0"))

    async def equity(self, price: float) -> float:
        price_d = Decimal(str(price))
        if self.settings.mode == "paper":
            return float(self._paper_quote_dec() + self._paper_base_dec() * price_d)

        balances = await self.exchange.asset_balances(
            {self.settings.quote_asset, self.settings.base_asset}
        )
        quote = Decimal(str(balances.get(self.settings.quote_asset, 0.0)))
        base = Decimal(str(balances.get(self.settings.base_asset, 0.0)))
        return float(quote + base * price_d)

    def _paper_execution_price(self, market_price: float, side: str) -> float:
        price = Decimal(str(market_price))
        slip = Decimal(str(self.settings.paper_slippage_bps)) / Decimal("10000")
        if side.upper() == "BUY":
            return float(price * (Decimal("1") + slip))
        return float(price * (Decimal("1") - slip))

    async def _risk_prices_from_fill(self, fill_price: float) -> tuple[float, float]:
        stop = fill_price * (1 - self.settings.stop_loss_pct)
        take = fill_price * (1 + self.settings.take_profit_pct)
        return (
            await self.exchange.normalize_price(self.settings.symbol, stop),
            await self.exchange.normalize_price(self.settings.symbol, take),
        )

    def _new_client_order_id(self, side: str) -> str:
        # <= 36 chars and unique enough for local idempotency/reconciliation.
        return f"agt-{side.lower()}-{uuid.uuid4().hex[:20]}"

    async def _submit_market_order(self, side: str, qty: float) -> tuple[dict, str, float]:
        client_order_id = self._new_client_order_id(side)
        logger.info(
            "%s submitting %s market order qty=%g id=%s",
            self.settings.symbol,
            side,
            qty,
            client_order_id,
        )
        self.db.record_order_intent(
            mode=self.settings.mode,
            symbol=self.settings.symbol,
            client_order_id=client_order_id,
            side=side,
            requested_quantity=qty,
        )

        try:
            order = await self.exchange.market_order(
                self.settings.symbol,
                side,
                qty,
                client_order_id=client_order_id,
            )
        except Exception as submit_error:
            # Never blindly re-submit after an uncertain network outcome. Query Binance
            # by our idempotent client order id first.
            try:
                order = await self.exchange.get_order(
                    self.settings.symbol,
                    client_order_id=client_order_id,
                )
            except Exception as reconcile_error:
                self.db.update_order_record(
                    client_order_id=client_order_id,
                    binance_order_id=None,
                    executed_quantity=0.0,
                    average_fill_price=None,
                    status="UNKNOWN",
                    commission_quote=0.0,
                    commission_details=[],
                )
                logger.error(
                    "%s order outcome uncertain id=%s; re-submission blocked "
                    "(submit error: %s; reconcile error: %s)",
                    self.settings.symbol,
                    client_order_id,
                    submit_error,
                    reconcile_error,
                )
                raise RuntimeError(
                    "Order outcome is uncertain. Automatic re-submission was blocked; "
                    f"manual/restart reconciliation is required. Submit error: {submit_error}; "
                    f"reconcile error: {reconcile_error}"
                ) from submit_error

        if (order.get("symbol") != self.settings.symbol or order.get("side") != side
                or order.get("clientOrderId") != client_order_id):
            logger.error(
                "%s order response identity mismatch id=%s; recovery required",
                self.settings.symbol,
                client_order_id,
            )
            raise RuntimeError("Order response identity mismatch; recovery is required")
        executed_qty = self.exchange.executed_quantity(order)
        if not math.isfinite(executed_qty) or executed_qty < 0 or executed_qty > qty * (1 + 1e-10):
            raise RuntimeError("Invalid executed quantity; recovery is required")
        fill_price = self.exchange.weighted_fill_price(order, 0.0) if executed_qty > 0 else None
        fee_quote = await self.exchange.order_fee_quote(order, fill_price or 0.0)
        details = self.exchange.commission_details(order)
        self.db.update_order_record(
            client_order_id=client_order_id,
            binance_order_id=str(order.get("orderId")) if order.get("orderId") is not None else None,
            executed_quantity=executed_qty,
            average_fill_price=fill_price,
            status=str(order.get("status") or "UNKNOWN"),
            commission_quote=fee_quote,
            commission_details=details,
        )
        if executed_qty > 0:
            fills = order.get("fills") or []
            complete_fills = (
                fills and all("commission" in fill and "commissionAsset" in fill for fill in fills)
                and math.isclose(sum(float(fill.get("qty", 0)) for fill in fills), executed_qty, rel_tol=1e-9)
            )
            if (order.get("status") not in {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}
                    or not complete_fills or fill_price is None or not math.isfinite(fill_price) or fill_price <= 0):
                raise RuntimeError("Fill details incomplete or order still active; recovery is required before further orders")
        return order, client_order_id, fee_quote

    async def reconcile_unresolved_orders(self) -> list[dict]:
        """Re-check uncertain exchange orders and apply only fully verified terminal fills."""
        if self.settings.mode == "paper":
            return []

        terminal = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}
        results = []
        for record in self.db.unresolved_orders(
            mode=self.settings.mode,
            symbol=self.settings.symbol,
        ):
            client_order_id = str(record["client_order_id"])
            try:
                order = await self.exchange.get_order(
                    self.settings.symbol,
                    client_order_id=client_order_id,
                )
            except Exception as exc:
                results.append({
                    "client_order_id": client_order_id,
                    "status": record["status"],
                    "resolved": False,
                    "error": str(exc),
                })
                continue

            executed_qty = self.exchange.executed_quantity(order)
            raw_status = str(order.get("status") or "UNKNOWN")
            if (
                order.get("symbol") != self.settings.symbol
                or order.get("side") != record["side"]
                or order.get("clientOrderId") != client_order_id
                or not math.isfinite(executed_qty)
                or executed_qty < float(record["executed_quantity"])
            ):
                results.append({
                    "client_order_id": client_order_id,
                    "resolved": False,
                    "error": "Invalid or mismatched reconciliation response",
                })
                continue

            if executed_qty <= 0:
                self.db.update_order_record(
                    client_order_id=client_order_id,
                    binance_order_id=(
                        str(order.get("orderId"))
                        if order.get("orderId") is not None else None
                    ),
                    executed_quantity=0.0,
                    average_fill_price=None,
                    status=raw_status,
                    commission_quote=0.0,
                    commission_details=[],
                )
                results.append({
                    "client_order_id": client_order_id,
                    "status": raw_status,
                    "resolved": raw_status in terminal | {"REJECTED"},
                    "executed_quantity": 0.0,
                })
                continue

            if raw_status not in terminal or order.get("orderId") is None:
                stored_status = "RECOVERY_REQUIRED"
                self.db.update_order_record(
                    client_order_id=client_order_id,
                    binance_order_id=(
                        str(order.get("orderId"))
                        if order.get("orderId") is not None else None
                    ),
                    executed_quantity=executed_qty,
                    average_fill_price=None,
                    status=stored_status,
                    commission_quote=float(record.get("commission_quote") or 0.0),
                    commission_details=json.loads(record.get("commission_details") or "[]"),
                )
                results.append({
                    "client_order_id": client_order_id,
                    "status": stored_status,
                    "resolved": False,
                    "executed_quantity": executed_qty,
                    "error": "Order is not terminal or does not have a Binance order id",
                })
                continue

            try:
                trades = await self.exchange.my_trades(
                    self.settings.symbol,
                    order_id=order["orderId"],
                )
            except Exception as exc:
                trades = []
                fill_lookup_error = str(exc)
            else:
                fill_lookup_error = None

            fills = [
                {
                    "price": str(item.get("price") or "0"),
                    "qty": str(item.get("qty") or "0"),
                    "commission": str(item.get("commission") or "0"),
                    "commissionAsset": str(item.get("commissionAsset") or ""),
                }
                for item in trades
                if str(item.get("orderId")) == str(order.get("orderId"))
            ]
            fill_qty = sum(float(item["qty"]) for item in fills)
            complete_fills = (
                bool(fills)
                and all(item["commissionAsset"] for item in fills)
                and math.isclose(fill_qty, executed_qty, rel_tol=1e-9, abs_tol=1e-12)
            )
            if not complete_fills:
                self.db.update_order_record(
                    client_order_id=client_order_id,
                    binance_order_id=str(order.get("orderId")),
                    executed_quantity=executed_qty,
                    average_fill_price=None,
                    status="RECOVERY_REQUIRED",
                    commission_quote=float(record.get("commission_quote") or 0.0),
                    commission_details=json.loads(record.get("commission_details") or "[]"),
                )
                results.append({
                    "client_order_id": client_order_id,
                    "status": "RECOVERY_REQUIRED",
                    "resolved": False,
                    "executed_quantity": executed_qty,
                    "error": (
                        "Complete Binance fills were unavailable"
                        + (f": {fill_lookup_error}" if fill_lookup_error else "")
                    ),
                })
                continue

            verified_order = dict(order)
            verified_order["fills"] = fills
            fill_price = self.exchange.weighted_fill_price(verified_order, 0.0)
            if not math.isfinite(fill_price) or fill_price <= 0:
                results.append({
                    "client_order_id": client_order_id,
                    "status": "RECOVERY_REQUIRED",
                    "resolved": False,
                    "executed_quantity": executed_qty,
                    "error": "Verified fills did not produce a valid average fill price",
                })
                continue

            fee_quote = await self.exchange.order_fee_quote(verified_order, fill_price)
            commission_details = self.exchange.commission_details(verified_order)
            self.db.update_order_record(
                client_order_id=client_order_id,
                binance_order_id=str(order.get("orderId")),
                executed_quantity=executed_qty,
                average_fill_price=fill_price,
                status=raw_status,
                commission_quote=fee_quote,
                commission_details=commission_details,
            )

            try:
                if record["side"] == "BUY":
                    if self.db.get_open_trade(self.settings.symbol, self.settings.mode):
                        raise RuntimeError(
                            "An open position already exists; recovered BUY cannot be applied safely"
                        )
                    base_commission = self._base_commission(
                        verified_order,
                        self.settings.base_asset,
                    )
                    held_qty = max(0.0, executed_qty - base_commission)
                    if held_qty <= 0:
                        raise RuntimeError("Recovered BUY left no usable base quantity")
                    stop_price, take_profit_price = await self._risk_prices_from_fill(
                        fill_price
                    )
                    self.db.open_trade(
                        mode=self.settings.mode,
                        symbol=self.settings.symbol,
                        quantity=held_qty,
                        entry_price=fill_price,
                        entry_fee=fee_quote,
                        reason="Recovered Binance BUY after restart",
                        stop_price=stop_price,
                        take_profit_price=take_profit_price,
                        client_order_id=client_order_id,
                    )
                    recovered_trade = self.db.get_open_trade(
                        self.settings.symbol,
                        self.settings.mode,
                    )
                    if recovered_trade is None:
                        raise RuntimeError(
                            "Recovered BUY was applied but the local position could not be reloaded"
                        )
                    # Do not leave a recovered exchange position temporarily
                    # unprotected until the next strategy/risk cycle. Install or
                    # reconcile exchange-resident OCO protection as part of the
                    # same startup recovery workflow.
                    await self._place_exchange_protection(recovered_trade)
                else:
                    trade = self.db.get_open_trade(
                        self.settings.symbol,
                        self.settings.mode,
                    )
                    if trade is None:
                        raise RuntimeError(
                            "No local open position exists for recovered SELL"
                        )
                    self.db.close_trade(
                        int(trade["id"]),
                        fill_price,
                        fee_quote,
                        "Recovered Binance SELL after restart",
                        executed_quantity=executed_qty,
                        client_order_id=client_order_id,
                    )
            except Exception as exc:
                results.append({
                    "client_order_id": client_order_id,
                    "status": "RECOVERY_REQUIRED",
                    "resolved": False,
                    "executed_quantity": executed_qty,
                    "error": str(exc),
                })
                continue

            results.append({
                "client_order_id": client_order_id,
                "status": raw_status,
                "resolved": True,
                "executed_quantity": executed_qty,
                "average_fill_price": fill_price,
            })
        return results

    @staticmethod
    def _base_commission(order: dict, base_asset: str) -> float:
        total = Decimal("0")
        for fill in order.get("fills") or []:
            if str(fill.get("commissionAsset") or "") == base_asset:
                total += Decimal(str(fill.get("commission") or "0"))
        return float(total)

    def _new_protection_ids(self) -> tuple[str, str, str]:
        token = uuid.uuid4().hex[:16]
        return (
            f"agt-prot-{token}",
            f"agt-tp-{token}",
            f"agt-sl-{token}",
        )

    def _validate_protective_oco_response(
        self,
        response: dict,
        *,
        list_id: str,
        take_profit_client_order_id: str,
        stop_client_order_id: str,
    ) -> tuple[int, str]:
        if not isinstance(response, dict):
            raise RuntimeError("Protective OCO response is not an object")
        if response.get("symbol") != self.settings.symbol:
            raise RuntimeError("Protective OCO response symbol mismatch")
        if response.get("listClientOrderId") != list_id:
            raise RuntimeError("Protective OCO response list client id mismatch")

        raw_order_list_id = response.get("orderListId")
        if isinstance(raw_order_list_id, bool):
            raise RuntimeError("Protective OCO response is missing a valid order-list id")
        try:
            order_list_id = int(raw_order_list_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(
                "Protective OCO response is missing a valid order-list id"
            ) from exc
        if order_list_id <= 0:
            raise RuntimeError("Protective OCO response is missing a valid order-list id")

        status = str(response.get("listOrderStatus") or "").strip().upper()
        if status not in {"EXECUTING", "ALL_DONE", "REJECT", "EXPIRED"}:
            raise RuntimeError(
                "Protective OCO response has an unverified list status "
                f"(listOrderStatus={status or 'missing'})"
            )

        orders = response.get("orders")
        if not isinstance(orders, list) or len(orders) != 2:
            raise RuntimeError(
                "Protective OCO response did not confirm exactly two child orders"
            )

        expected = {take_profit_client_order_id, stop_client_order_id}
        seen_client_ids: set[str] = set()
        seen_order_ids: set[int] = set()
        expected_types = {
            take_profit_client_order_id: "LIMIT_MAKER",
            stop_client_order_id: "STOP_LOSS",
        }
        for child in orders:
            if not isinstance(child, dict):
                raise RuntimeError("Protective OCO response contains an invalid child order")
            if child.get("symbol") != self.settings.symbol:
                raise RuntimeError("Protective OCO child order symbol mismatch")
            client_id = str(child.get("clientOrderId") or "")
            if client_id not in expected or client_id in seen_client_ids:
                raise RuntimeError("Protective OCO response child client ids do not match")
            seen_client_ids.add(client_id)
            raw_child_order_id = child.get("orderId")
            if isinstance(raw_child_order_id, bool):
                raise RuntimeError("Protective OCO child order is missing a valid order id")
            try:
                child_order_id = int(raw_child_order_id)
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError(
                    "Protective OCO child order is missing a valid order id"
                ) from exc
            if child_order_id <= 0 or child_order_id in seen_order_ids:
                raise RuntimeError("Protective OCO child order ids are invalid or duplicated")
            seen_order_ids.add(child_order_id)
            if child.get("side") and str(child["side"]).upper() != "SELL":
                raise RuntimeError("Protective OCO child order is not a SELL")
            child_type = child.get("type") or child.get("origType")
            if child_type and str(child_type).upper() != expected_types[client_id]:
                raise RuntimeError("Protective OCO child order type does not match its leg")

        if seen_client_ids != expected:
            raise RuntimeError("Protective OCO response is missing a take-profit or stop leg")
        return order_list_id, status

    def _validate_protective_order_list(
        self,
        order_list: dict,
        *,
        trade: dict,
    ) -> tuple[int, str, list[dict]]:
        if not isinstance(order_list, dict):
            raise RuntimeError("Protective order-list query is not an object")
        expected_symbol = str(trade.get("symbol") or self.settings.symbol)
        if order_list.get("symbol") != expected_symbol:
            raise RuntimeError("Protective order-list symbol mismatch")
        list_client_id = str(trade.get("protective_list_client_order_id") or "")
        if order_list.get("listClientOrderId") != list_client_id:
            raise RuntimeError("Protective order-list client id mismatch")
        raw_order_list_id = order_list.get("orderListId")
        if isinstance(raw_order_list_id, bool):
            raise RuntimeError("Protective order-list id is invalid")
        try:
            order_list_id = int(raw_order_list_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("Protective order-list id is invalid") from exc
        if order_list_id <= 0:
            raise RuntimeError("Protective order-list id is invalid")
        stored_list_id = trade.get("protective_order_list_id")
        if stored_list_id is not None and str(stored_list_id) != str(order_list_id):
            raise RuntimeError("Protective order-list id does not match persisted identity")

        status = str(order_list.get("listOrderStatus") or "").strip().upper()
        if status not in {"EXECUTING", "ALL_DONE", "REJECT", "EXPIRED"}:
            raise RuntimeError(
                f"Protective order-list status is not verifiable: {status or 'missing'}"
            )

        children = order_list.get("orders")
        if not isinstance(children, list) or len(children) != 2:
            raise RuntimeError(
                "Protective order list does not contain exactly two child orders"
            )
        token = None
        prefix = "agt-prot-"
        if list_client_id.startswith(prefix) and len(list_client_id) > len(prefix):
            token = list_client_id[len(prefix):]
        expected_ids = (
            {f"agt-tp-{token}", f"agt-sl-{token}"}
            if token is not None
            else None
        )
        expected_types = (
            {f"agt-tp-{token}": "LIMIT_MAKER", f"agt-sl-{token}": "STOP_LOSS"}
            if token is not None
            else {}
        )
        seen_client_ids: set[str] = set()
        seen_order_ids: set[int] = set()
        normalized_children = []
        for child in children:
            if not isinstance(child, dict) or child.get("symbol") != expected_symbol:
                raise RuntimeError("Protective child order is missing or has the wrong symbol")
            client_id = str(child.get("clientOrderId") or "")
            if not client_id or client_id in seen_client_ids:
                raise RuntimeError("Protective child order client ids are missing or duplicated")
            seen_client_ids.add(client_id)
            raw_child_id = child.get("orderId")
            if isinstance(raw_child_id, bool):
                raise RuntimeError("Protective child order id is invalid")
            try:
                child_id = int(raw_child_id)
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("Protective child order id is invalid") from exc
            if child_id <= 0 or child_id in seen_order_ids:
                raise RuntimeError("Protective child order ids are invalid or duplicated")
            seen_order_ids.add(child_id)
            if child.get("side") and str(child["side"]).upper() != "SELL":
                raise RuntimeError("Protective child order is not a SELL")
            child_type = child.get("type") or child.get("origType")
            expected_type = expected_types.get(client_id)
            if expected_type and child_type and str(child_type).upper() != expected_type:
                raise RuntimeError("Protective child order type does not match its leg")
            normalized_children.append(child)

        if expected_ids is not None and seen_client_ids != expected_ids:
            raise RuntimeError("Protective order list is missing an expected child leg")
        # Query Order List may return only symbol/order/client ids. For older
        # arbitrary client ids, the verified two-child structure is the
        # strongest identity available after restart.
        return order_list_id, status, normalized_children

    async def _place_exchange_protection(
        self,
        trade: dict,
        *,
        _terminal_retries: int = 0,
    ) -> dict:
        if self.settings.mode not in {"testnet", "live"}:
            return {"protected": False, "status": "NOT_APPLICABLE"}
        if trade.get("protective_list_client_order_id"):
            status = trade.get("protection_status") or "UNKNOWN"
            return {
                "protected": (
                    status == "EXECUTING"
                    and bool(trade.get("protective_order_list_id"))
                ),
                "status": status,
                "list_client_order_id": trade["protective_list_client_order_id"],
                "order_list_id": trade.get("protective_order_list_id"),
            }

        quantity = float(trade["quantity"])
        take_profit = float(trade["take_profit_price"])
        stop_price = float(trade["stop_price"])
        list_id, take_id, stop_id = self._new_protection_ids()
        trade_id = int(trade["id"])

        # Persist the idempotency key before sending the request. If the process
        # dies after Binance accepts the OCO, startup can query this exact list
        # instead of submitting a second pair of SELL orders.
        self.db.set_trade_protection(
            trade_id,
            order_list_id=None,
            list_client_order_id=list_id,
            status="PROTECTION_SUBMITTING",
        )
        try:
            response = await self.exchange.place_protective_oco(
                self.settings.symbol,
                quantity=quantity,
                take_profit_price=take_profit,
                stop_price=stop_price,
                list_client_order_id=list_id,
                take_profit_client_order_id=take_id,
                stop_client_order_id=stop_id,
            )
        except Exception as submit_error:
            try:
                response = await self.exchange.get_order_list(
                    self.settings.symbol,
                    list_client_order_id=list_id,
                )
            except Exception as reconcile_error:
                self.db.set_trade_protection(
                    trade_id,
                    order_list_id=None,
                    list_client_order_id=list_id,
                    status="PROTECTION_UNKNOWN",
                )
                raise RuntimeError(
                    "Protective OCO outcome is uncertain; position requires "
                    f"reconciliation. Submit error: {submit_error}; "
                    f"reconcile error: {reconcile_error}"
                ) from submit_error

        try:
            order_list_id, status = self._validate_protective_oco_response(
                response,
                list_id=list_id,
                take_profit_client_order_id=take_id,
                stop_client_order_id=stop_id,
            )
        except Exception as exc:
            self.db.set_trade_protection(
                trade_id,
                order_list_id=None,
                list_client_order_id=list_id,
                status="PROTECTION_UNKNOWN",
            )
            raise RuntimeError(
                "Protective OCO response could not be verified; the submitted list "
                "will not be blindly retried and requires reconciliation: "
                f"{exc}"
            ) from exc

        self.db.set_trade_protection(
            trade_id,
            order_list_id=order_list_id,
            list_client_order_id=list_id,
            status=status,
        )
        if status == "EXECUTING":
            return {
                "protected": True,
                "status": status,
                "list_client_order_id": list_id,
                "order_list_id": order_list_id,
            }

        # An OCO can become terminal before the placement response is handled.
        # Re-query by the already-persisted idempotency key and apply any fill
        # using complete account-trade data; never call this a protected entry
        # or retry a terminal/uncertain list blindly.
        persisted_trade = self.db.get_open_trade(
            self.settings.symbol,
            self.settings.mode,
        )
        try:
            fill = (
                await self._protective_fill(persisted_trade)
                if persisted_trade is not None
                else None
            )
        except Exception as exc:
            self.db.set_trade_protection(
                trade_id,
                order_list_id=order_list_id,
                list_client_order_id=list_id,
                status="PROTECTION_UNKNOWN",
            )
            raise RuntimeError(
                "Terminal protective OCO could not be reconciled; the list will "
                f"not be resubmitted: {exc}"
            ) from exc
        refreshed_trade = self.db.get_open_trade(
            self.settings.symbol,
            self.settings.mode,
        )
        if refreshed_trade is None:
            return {
                "protected": False,
                "status": status,
                "list_client_order_id": list_id,
                "order_list_id": order_list_id,
                "reconciled_fill": fill,
            }
        if _terminal_retries >= 1:
            self.db.set_trade_protection(
                trade_id,
                order_list_id=order_list_id,
                list_client_order_id=list_id,
                status="PROTECTION_UNKNOWN",
            )
            raise RuntimeError(
                "A replacement protective OCO also became terminal; position "
                "requires manual reconciliation"
            )

        # The terminal list and both child outcomes were fully queried above,
        # including any account fills. It is now safe to retire this identity
        # and protect only the still-open residual quantity with one new list.
        replacement = await self._rearm_terminal_protection(
            refreshed_trade,
            fill=fill,
            retries=_terminal_retries + 1,
        )
        return replacement

    def _validate_protective_child_order(
        self,
        order: dict,
        *,
        child: dict,
        symbol: str | None = None,
    ) -> None:
        if not isinstance(order, dict):
            raise RuntimeError("Protective child-order query is not an object")
        expected_symbol = symbol or self.settings.symbol
        client_id = str(child.get("clientOrderId") or "")
        if order.get("symbol") != expected_symbol:
            raise RuntimeError("Protective child-order symbol mismatch")
        if str(order.get("clientOrderId") or "") != client_id:
            raise RuntimeError("Protective child-order client id mismatch")
        if str(order.get("orderId")) != str(child.get("orderId")):
            raise RuntimeError("Protective child-order id mismatch")
        if str(order.get("side") or "").upper() != "SELL":
            raise RuntimeError("Protective child order is not a SELL")
        status = str(order.get("status") or "").strip().upper()
        if status not in {
            "NEW",
            "PENDING_NEW",
            "PARTIALLY_FILLED",
            "FILLED",
            "PENDING_CANCEL",
            "CANCELED",
            "REJECTED",
            "EXPIRED",
            "EXPIRED_IN_MATCH",
        }:
            raise RuntimeError(
                f"Protective child-order status is not verifiable: {status or 'missing'}"
            )

        child_type = str(order.get("type") or order.get("origType") or "").upper()
        token = None
        for prefix in ("agt-tp-", "agt-sl-"):
            if client_id.startswith(prefix):
                token = client_id[len(prefix):]
                break
        expected_type = None
        if token:
            if client_id == f"agt-tp-{token}":
                expected_type = "LIMIT_MAKER"
            elif client_id == f"agt-sl-{token}":
                expected_type = "STOP_LOSS"
        if expected_type and child_type != expected_type:
            raise RuntimeError("Protective child-order type does not match its leg")
        if not expected_type and child_type not in {
            "LIMIT_MAKER",
            "STOP_LOSS",
            "STOP_LOSS_LIMIT",
            "TAKE_PROFIT",
            "TAKE_PROFIT_LIMIT",
        }:
            raise RuntimeError("Protective child-order type is not verifiable")

    async def inspect_exchange_protection(self, trade: dict) -> dict:
        """Read-only verification of an existing OCO and both child-order states."""
        list_client_id = trade.get("protective_list_client_order_id")
        if not list_client_id:
            return {"verified": False, "protected": False, "status": "MISSING_ID"}
        symbol = str(trade.get("symbol") or self.settings.symbol)
        order_list = await self.exchange.get_order_list(
            symbol,
            list_client_order_id=str(list_client_id),
        )
        order_list_id, status, children = self._validate_protective_order_list(
            order_list,
            trade=trade,
        )
        child_states = []
        for child in children:
            order = await self.exchange.get_order(
                symbol,
                client_order_id=str(child["clientOrderId"]),
            )
            self._validate_protective_child_order(
                order,
                child=child,
                symbol=symbol,
            )
            if order.get("executedQty") is None:
                raise RuntimeError("Protective child-order execution quantity is missing")
            executed_qty = self.exchange.executed_quantity(order)
            if not math.isfinite(executed_qty) or executed_qty < 0:
                raise RuntimeError("Protective child-order execution quantity is invalid")
            child_states.append({
                "client_order_id": str(order["clientOrderId"]),
                "order_id": int(order["orderId"]),
                "status": str(order["status"]).strip().upper(),
                "executed_quantity": executed_qty,
            })

        active_statuses = {"NEW", "PARTIALLY_FILLED"}
        terminal_statuses = {
            "FILLED",
            "CANCELED",
            "REJECTED",
            "EXPIRED",
            "EXPIRED_IN_MATCH",
        }
        if status == "EXECUTING":
            child_states_consistent = all(
                item["status"] in active_statuses
                for item in child_states
            )
        else:
            child_states_consistent = all(
                item["status"] in terminal_statuses
                for item in child_states
            )
        protected = (
            status == "EXECUTING"
            and child_states_consistent
            and all(
                item["status"] == "NEW" and item["executed_quantity"] == 0
                for item in child_states
            )
        )
        return {
            "verified": child_states_consistent,
            "protected": protected,
            "status": status,
            "order_list_id": order_list_id,
            "list_client_order_id": str(list_client_id),
            "children": child_states,
        }

    async def _rearm_terminal_protection(
        self,
        trade: dict,
        *,
        fill: dict | None = None,
        retries: int = 1,
    ) -> dict:
        status = str(trade.get("protection_status") or "").strip().upper()
        if status not in {"ALL_DONE", "REJECT", "EXPIRED"}:
            raise RuntimeError(
                f"Cannot replace non-terminal protective list with status {status or 'UNKNOWN'}"
            )
        # Call only after _protective_fill validated the list, both children,
        # and all cumulative fills. This keeps a possibly accepted live OCO's
        # identity intact unless Binance has confirmed it is terminal.
        self.db.clear_trade_protection(
            int(trade["id"]),
            status="TERMINAL_LIST_RECONCILED",
        )
        refreshed = self.db.get_open_trade(
            self.settings.symbol,
            self.settings.mode,
        )
        if refreshed is None:
            return {
                "protected": False,
                "status": "POSITION_CLOSED",
                "reconciled_fill": fill,
            }

        replacement = await self._place_exchange_protection(
            refreshed,
            _terminal_retries=retries,
        )
        if fill is not None:
            replacement = {**replacement, "reconciled_fill": fill}
        if (
            self.db.get_open_trade(self.settings.symbol, self.settings.mode) is not None
            and not replacement.get("protected")
        ):
            raise RuntimeError(
                "Terminal protective list was reconciled but the remaining position "
                "did not receive active exchange protection"
            )
        return replacement

    async def _protective_fill(self, trade: dict) -> dict | None:
        list_client_id = trade.get("protective_list_client_order_id")
        if not list_client_id or self.settings.mode not in {"testnet", "live"}:
            return None
        symbol = str(trade.get("symbol") or self.settings.symbol)
        order_list = await self.exchange.get_order_list(
            symbol,
            list_client_order_id=str(list_client_id),
        )
        try:
            order_list_id, list_status, children = self._validate_protective_order_list(
                order_list,
                trade=trade,
            )
        except Exception as exc:
            self.db.set_trade_protection(
                int(trade["id"]),
                order_list_id=trade.get("protective_order_list_id"),
                list_client_order_id=str(list_client_id),
                status="PROTECTION_UNKNOWN",
            )
            raise RuntimeError(
                "Protective order list could not be verified safely: "
                f"{exc}"
            ) from exc
        self.db.set_trade_protection(
            int(trade["id"]),
            order_list_id=order_list_id,
            list_client_order_id=str(list_client_id),
            status=list_status,
        )
        list_base_qty = float(trade.get("protective_list_base_qty") or 0.0)
        list_base_quote = float(trade.get("protective_list_base_quote") or 0.0)
        list_base_fee = float(trade.get("protective_list_base_fee_quote") or 0.0)
        if any(
            not math.isfinite(value) or value < 0
            for value in (list_base_qty, list_base_quote, list_base_fee)
        ):
            self.db.set_trade_protection(
                int(trade["id"]),
                order_list_id=order_list_id,
                list_client_order_id=str(list_client_id),
                status="PROTECTION_UNKNOWN",
            )
            raise RuntimeError("Protective-list accounting baseline is invalid")

        active_child_statuses = {"NEW", "PARTIALLY_FILLED"}
        terminal_child_statuses = {
            "FILLED",
            "CANCELED",
            "REJECTED",
            "EXPIRED",
            "EXPIRED_IN_MATCH",
        }
        child_states: list[tuple[dict, float, str]] = []
        for child in children:
            client_id = str(child["clientOrderId"])
            try:
                order = await self.exchange.get_order(
                    symbol,
                    client_order_id=client_id,
                )
                self._validate_protective_child_order(
                    order,
                    child=child,
                    symbol=symbol,
                )
                if order.get("executedQty") is None:
                    raise RuntimeError("Protective child-order execution quantity is missing")
                executed_qty = self.exchange.executed_quantity(order)
                if not math.isfinite(executed_qty) or executed_qty < 0:
                    raise RuntimeError("Protective child-order execution quantity is invalid")
                order_status = str(order.get("status") or "").strip().upper()
                if order_status == "FILLED" and executed_qty <= 0:
                    raise RuntimeError("Filled protective child reports no execution quantity")
                if order_status == "PARTIALLY_FILLED" and executed_qty <= 0:
                    raise RuntimeError("Partial protective child reports no execution quantity")
                if order_status == "NEW" and executed_qty > 0:
                    raise RuntimeError("New protective child reports an execution quantity")
            except Exception as exc:
                self.db.set_trade_protection(
                    int(trade["id"]),
                    order_list_id=order_list_id,
                    list_client_order_id=str(list_client_id),
                    status="PROTECTION_UNKNOWN",
                )
                raise RuntimeError(
                    "Protective child order could not be verified safely: "
                    f"{exc}"
                ) from exc
            child_states.append((order, executed_qty, order_status))

        terminal_list = list_status in {"ALL_DONE", "REJECT", "EXPIRED"}
        if terminal_list and any(
            status not in terminal_child_statuses
            for _, _, status in child_states
        ):
            self.db.set_trade_protection(
                int(trade["id"]),
                order_list_id=order_list_id,
                list_client_order_id=str(list_client_id),
                status="PROTECTION_UNKNOWN",
            )
            raise RuntimeError(
                "Terminal protective list still has a non-terminal child order"
            )

        executing_pair = (
            list_status == "EXECUTING"
            and all(status in active_child_statuses for _, _, status in child_states)
        )
        executed_children = [
            (order, executed_qty, status)
            for order, executed_qty, status in child_states
            if executed_qty > 0
        ]
        if len(executed_children) > 1:
            self.db.set_trade_protection(
                int(trade["id"]),
                order_list_id=order_list_id,
                list_client_order_id=str(list_client_id),
                status="PROTECTION_UNKNOWN",
            )
            raise RuntimeError(
                "Both protective OCO legs report executions; position requires manual reconciliation"
            )
        if not executed_children:
            if list_status == "EXECUTING" and not executing_pair:
                self.db.set_trade_protection(
                    int(trade["id"]),
                    order_list_id=order_list_id,
                    list_client_order_id=str(list_client_id),
                    status="PROTECTION_UNKNOWN",
                )
                raise RuntimeError(
                    "Executing protective list does not have both active child orders"
                )
            return None

        order, executed_qty, order_status = executed_children[0]
        try:
            trades = await self.exchange.my_trades(
                symbol,
                order_id=order["orderId"],
            )
            if not isinstance(trades, list):
                raise RuntimeError("Binance trade-fill response is not a list")
            fills = []
            for item in trades:
                if not isinstance(item, dict):
                    raise RuntimeError("Binance trade-fill entry is not an object")
                if str(item.get("orderId")) != str(order.get("orderId")):
                    continue
                if item.get("symbol") and item.get("symbol") != symbol:
                    raise RuntimeError("Protective account fill symbol mismatch")
                price_text = str(item.get("price") or "0")
                quantity_text = str(item.get("qty") or "0")
                commission_text = str(item.get("commission") or "0")
                try:
                    fill_price_value = float(price_text)
                    fill_quantity_value = float(quantity_text)
                    commission_value = float(commission_text)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise RuntimeError("Protective account fill contains invalid numbers") from exc
                if (
                    not math.isfinite(fill_price_value)
                    or fill_price_value <= 0
                    or not math.isfinite(fill_quantity_value)
                    or fill_quantity_value <= 0
                    or not math.isfinite(commission_value)
                    or commission_value < 0
                ):
                    raise RuntimeError("Protective account fill contains invalid values")
                fills.append({
                    "price": price_text,
                    "qty": quantity_text,
                    "commission": commission_text,
                    "commissionAsset": str(item.get("commissionAsset") or ""),
                })
            fill_qty = sum(float(item["qty"]) for item in fills)
            if (
                not fills
                or not math.isfinite(fill_qty)
                or not math.isclose(
                    fill_qty,
                    executed_qty,
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                )
            ):
                raise RuntimeError(
                    "Protective order executed but complete Binance fills are "
                    "not yet available; local position remains blocked for reconciliation"
                )

            cumulative_quote = sum(
                float(item["price"]) * float(item["qty"]) for item in fills
            )
            if not math.isfinite(cumulative_quote) or cumulative_quote <= 0:
                raise RuntimeError("Protective cumulative quote could not be verified")

            verified = dict(order)
            verified["fills"] = fills
            fill_price = self.exchange.weighted_fill_price(
                verified,
                0.0,
            )
            if not math.isfinite(fill_price) or fill_price <= 0:
                raise RuntimeError("Protective fill price could not be verified")
            fee = await self.exchange.order_fee_quote(verified, fill_price)
            reason = (
                "Exchange take profit"
                if "TAKE_PROFIT" in str(order.get("type"))
                or (
                    float(order.get("price") or 0) > 0
                    and fill_price >= float(trade["entry_price"])
                )
                else "Exchange stop loss"
            )
            applied = self.db.apply_protective_execution(
                int(trade["id"]),
                cumulative_quantity=list_base_qty + executed_qty,
                cumulative_quote=list_base_quote + cumulative_quote,
                cumulative_fee_quote=list_base_fee + fee,
                reason=reason,
            )
        except Exception as exc:
            self.db.set_trade_protection(
                int(trade["id"]),
                order_list_id=order_list_id,
                list_client_order_id=str(list_client_id),
                status="PROTECTION_UNKNOWN",
            )
            raise RuntimeError(
                "Protective execution could not be reconciled safely: "
                f"{exc}"
            ) from exc

        actively_partial = (
            order_status == "PARTIALLY_FILLED"
            and list_status == "EXECUTING"
            and executing_pair
        )
        if list_status == "EXECUTING" and not executing_pair:
            refreshed = self.db.get_open_trade(
                self.settings.symbol,
                self.settings.mode,
            )
            if refreshed is not None:
                self.db.set_trade_protection(
                    int(trade["id"]),
                    order_list_id=order_list_id,
                    list_client_order_id=str(list_client_id),
                    status="PROTECTION_UNKNOWN",
                )
                raise RuntimeError(
                    "A protective leg executed but both OCO legs are not confirmed active"
                )

        if applied is None:
            return {
                "closed": None,
                "fill_price": fill_price,
                "exit_fee_quote": fee,
                "order": order,
                "reason": reason,
                "partial_pending": actively_partial,
                "applied": False,
            }
        if not applied.get("applied"):
            if actively_partial:
                return {
                    "closed": applied,
                    "fill_price": fill_price,
                    "exit_fee_quote": fee,
                    "order": order,
                    "reason": reason,
                    "partial_pending": True,
                    "applied": False,
                }
            return None
        return {
            "closed": applied,
            "fill_price": fill_price,
            "exit_fee_quote": fee,
            "order": order,
            "reason": reason,
            "partial_pending": bool(
                applied.get("partial") and actively_partial
            ),
            "applied": True,
        }

    async def reconcile_open_position_protection(self) -> dict:
        """Verify or install exchange protection before the trading loop starts."""
        trade = self.db.get_open_trade(self.settings.symbol, self.settings.mode)
        if trade is None or self.settings.mode not in {"testnet", "live"}:
            return {"status": "NO_OPEN_POSITION"}

        if trade.get("protective_list_client_order_id"):
            fill = await self._protective_fill(trade)
            refreshed = self.db.get_open_trade(
                self.settings.symbol,
                self.settings.mode,
            )
            if refreshed is None:
                return {
                    "status": "EXCHANGE_FILL_RECONCILED",
                    "fill": fill,
                }
            status = str(refreshed.get("protection_status") or "UNKNOWN").upper()
            if status == "EXECUTING":
                if fill is not None:
                    return {
                        "status": "EXCHANGE_FILL_RECONCILED",
                        "protection_status": status,
                        "fill": fill,
                    }
                return {
                    "status": "PROTECTED",
                    "protection_status": status,
                    "list_client_order_id": refreshed.get(
                        "protective_list_client_order_id"
                    ),
                    "order_list_id": refreshed.get("protective_order_list_id"),
                }
            if status in {"ALL_DONE", "REJECT", "EXPIRED"}:
                replacement = await self._rearm_terminal_protection(
                    refreshed,
                    fill=fill,
                )
                if self.db.get_open_trade(
                    self.settings.symbol,
                    self.settings.mode,
                ) is None:
                    return {
                        "status": "EXCHANGE_FILL_RECONCILED",
                        "fill": fill or replacement.get("reconciled_fill"),
                    }
                if not replacement.get("protected"):
                    raise RuntimeError(
                        "Terminal OCO was reconciled but replacement protection "
                        "is not active"
                    )
                return {
                    **replacement,
                    "startup_status": "TERMINAL_OCO_RECONCILED_AND_REPROTECTED",
                }
            raise RuntimeError(
                "Open position protection could not be verified safely at startup: "
                f"{status}"
            )

        protection = await self._place_exchange_protection(trade)
        if str(protection.get("status") or "") != "EXECUTING":
            raise RuntimeError(
                "Open position did not receive active exchange protection at startup"
            )
        return {
            **protection,
            "startup_status": "PROTECTION_INSTALLED",
        }

    async def _cancel_exchange_protection(self, trade: dict) -> dict | None:
        list_client_id = trade.get("protective_list_client_order_id")
        if self.settings.mode not in {"testnet", "live"} or not list_client_id:
            return None
        try:
            await self.exchange.cancel_order_list(
                self.settings.symbol,
                list_client_order_id=str(list_client_id),
            )
        except Exception as cancel_error:
            fill = await self._protective_fill(trade)
            if fill is not None:
                return fill
            status = str(
                self.db.get_open_trade(
                    self.settings.symbol,
                    self.settings.mode,
                ).get("protection_status")
                or ""
            )
            if status not in {"ALL_DONE", "REJECT", "EXPIRED"}:
                raise cancel_error
            self.db.clear_trade_protection(
                int(trade["id"]),
                status="CANCELED_FOR_SOFTWARE_EXIT",
            )
            return None

        # A leg can fill at the same moment cancellation reaches the exchange.
        # Reconcile the list before submitting any replacement market SELL.
        fill = await self._protective_fill(trade)
        if fill is not None:
            return fill
        self.db.clear_trade_protection(
            int(trade["id"]),
            status="CANCELED_FOR_SOFTWARE_EXIT",
        )
        return None

    async def enter(self, signal: StrategySignal, risk: RiskDecision, price: float) -> ExecutionResult:
        if signal.side != SignalSide.BUY:
            return ExecutionResult("BUY", False, "Entry requires a BUY signal")
        if (not math.isfinite(price) or price <= 0 or not math.isfinite(risk.quantity)
                or not math.isfinite(signal.confidence) or not 0 <= signal.confidence <= 1):
            return ExecutionResult("BUY", False, "Invalid entry price or quantity")
        if not risk.allowed or risk.quantity <= 0:
            return ExecutionResult("BUY", False, risk.reason)
        if self.db.has_unresolved_order(mode=self.settings.mode, symbol=self.settings.symbol):
            return ExecutionResult(
                "BUY",
                False,
                "An earlier Binance order has an unresolved outcome; new entries are blocked",
            )
        if self.db.get_open_trade(self.settings.symbol, self.settings.mode):
            return ExecutionResult("BUY", False, "Position already open")

        qty = await self.exchange.normalize_quantity(self.settings.symbol, risk.quantity, price)
        if not math.isfinite(qty) or qty <= 0 or qty > risk.quantity:
            return ExecutionResult("BUY", False, "Quantity violates Binance market quantity/notional filters")

        if self.settings.mode == "paper":
            fill_price = self._paper_execution_price(price, "BUY")
            price_d = Decimal(str(fill_price))
            qty_d = Decimal(str(qty))
            fee_rate = Decimal(str(self.settings.trading_fee_bps)) / Decimal("10000")
            fee = price_d * qty_d * fee_rate
            cost = price_d * qty_d + fee
            stop_price, take_profit_price = await self._risk_prices_from_fill(fill_price)
            # Read shared cash after the final await, immediately before committing.
            quote = self._paper_quote_dec()
            base = self._paper_base_dec()
            if cost > quote:
                return ExecutionResult("BUY", False, "Insufficient paper quote balance")

            trade_id = self.db.open_trade(
                mode=self.settings.mode,
                symbol=self.settings.symbol,
                quantity=float(qty_d),
                entry_price=float(price_d),
                entry_fee=float(fee),
                reason=signal.reason,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                state_updates={
                    self._paper_quote_key: quote - cost,
                    self._paper_base_key: base + qty_d,
                },
            )
            return ExecutionResult(
                "BUY",
                True,
                "Paper position opened",
                {
                    "trade_id": trade_id,
                    "qty": float(qty_d),
                    "market_price": price,
                    "fill_price": float(price_d),
                    "estimated_slippage_bps": self.settings.paper_slippage_bps,
                    "entry_fee": float(fee),
                },
            )

        if self.settings.mode == "live" and not self.settings.allow_live_trading:
            return ExecutionResult("BUY", False, "Live trading blocked: ALLOW_LIVE_TRADING=false")

        try:
            await self.exchange.check_entry_liquidity(self.settings.symbol, qty, price)
        except ValueError as exc:
            return ExecutionResult("BUY", False, str(exc), {"order_submitted": False})
        order, client_order_id, fee = await self._submit_market_order("BUY", qty)
        executed_qty = self.exchange.executed_quantity(order)
        if executed_qty <= 0:
            return ExecutionResult(
                "BUY",
                False,
                f"Binance order {client_order_id} did not report an executed quantity",
                {"order": order},
            )

        fill_price = self.exchange.weighted_fill_price(order, price)
        base_commission = self._base_commission(order, self.settings.base_asset)
        raw_held_qty = max(0.0, executed_qty - base_commission)
        held_qty = await self.exchange.normalize_quantity(
            self.settings.symbol,
            raw_held_qty,
            fill_price,
        )
        if held_qty <= 0:
            raise RuntimeError(
                "Executed buy left no exchange-tradable base quantity; "
                "manual reconciliation is required"
            )

        stop_price, take_profit_price = await self._risk_prices_from_fill(fill_price)
        trade_id = self.db.open_trade(
            mode=self.settings.mode,
            symbol=self.settings.symbol,
            quantity=held_qty,
            entry_price=fill_price,
            entry_fee=fee,
            reason=signal.reason,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            client_order_id=client_order_id,
        )
        protection = None
        if self.settings.mode in {"testnet", "live"}:
            protection = await self._place_exchange_protection(
                self.db.get_open_trade(self.settings.symbol, self.settings.mode)
            )
        return ExecutionResult(
            "BUY",
            True,
            "Binance market buy executed",
            {
                "trade_id": trade_id,
                "client_order_id": client_order_id,
                "requested_qty": qty,
                "executed_qty": executed_qty,
                "recorded_position_qty": held_qty,
                "fill_price": fill_price,
                "entry_fee_quote": fee,
                "fee_valuation": order.get("_commission_quote_valuation"),
                "commissions": self.exchange.commission_details(order),
                "order": order,
                "protection": protection,
            },
        )

    async def _position_inventory_issue(self, trade: dict) -> str | None:
        """Detect exchange/local position drift without silently rewriting accounting."""
        if self.settings.mode == "paper":
            return None
        now = time.monotonic()
        if now - self._inventory_checked_at < self.settings.account_refresh_seconds:
            return self._inventory_issue

        details = await self.exchange.asset_balance_details(self.settings.base_asset)
        self._inventory_checked_at = now
        recorded = float(trade["quantity"])
        tolerance = max(1e-12, recorded * 1e-8)
        total = float(details["total"])
        free = float(details["free"])
        locked = float(details["locked"])

        if total + tolerance < recorded:
            self._inventory_issue = (
                f"Position reconciliation required: local quantity {recorded:.12g} "
                f"{self.settings.base_asset} exceeds exchange total {total:.12g}. "
                "Trading is blocked for this position until account/order history is reconciled."
            )
        elif (
            free + tolerance < recorded
            and not trade.get("protective_list_client_order_id")
        ):
            self._inventory_issue = (
                f"Position reconciliation required: {locked:.12g} "
                f"{self.settings.base_asset} is locked outside the bot's local position state. "
                "Trading is blocked until the lock/order is reconciled."
            )
        else:
            self._inventory_issue = None
        return self._inventory_issue

    async def maybe_exit(self, signal: StrategySignal, price: float) -> ExecutionResult:
        if not math.isfinite(price) or price <= 0:
            return ExecutionResult("HOLD", False, "Invalid exit price; awaiting valid market data")
        trade = self.db.get_open_trade(self.settings.symbol, self.settings.mode)
        if not trade:
            return ExecutionResult("HOLD", True, "No open position")
        if self.db.has_unresolved_order(mode=self.settings.mode, symbol=self.settings.symbol):
            return ExecutionResult(
                "HOLD",
                False,
                "An earlier Binance order has an unresolved outcome; duplicate exit submission is blocked",
            )

        if (
            self.settings.mode in {"testnet", "live"}
            and trade.get("protective_list_client_order_id")
        ):
            protective_fill = await self._protective_fill(trade)
            if protective_fill is not None:
                if protective_fill.get("partial_pending") and not protective_fill.get("applied"):
                    return ExecutionResult(
                        "HOLD",
                        True,
                        "Exchange protective order partially filled; remaining quantity is still protected",
                        protective_fill,
                    )
                return ExecutionResult(
                    "SELL",
                    True,
                    protective_fill["reason"],
                    protective_fill,
                )
            trade = self.db.get_open_trade(self.settings.symbol, self.settings.mode)
            if trade is None:
                return ExecutionResult("SELL", True, "Exchange protective fill reconciled")

        reason = None
        original_stop = float(trade["stop_price"])
        if price <= original_stop:
            reason = "Stop loss"
        elif price >= float(trade["take_profit_price"]):
            reason = "Take profit"
        elif signal.side == SignalSide.SELL:
            reason = signal.reason

        if (
            reason is None
            and self.settings.mode in {"testnet", "live"}
            and trade.get("protective_list_client_order_id")
            and str(trade.get("protection_status") or "").upper()
            in {"ALL_DONE", "REJECT", "EXPIRED"}
        ):
            replacement = await self._rearm_terminal_protection(trade)
            refreshed = self.db.get_open_trade(
                self.settings.symbol,
                self.settings.mode,
            )
            if refreshed is None:
                return ExecutionResult(
                    "SELL",
                    True,
                    "Exchange protective fill closed the position during reconciliation",
                    replacement,
                )
            if not replacement.get("protected"):
                return ExecutionResult(
                    "HOLD",
                    False,
                    "Terminal exchange protection was reconciled but the remaining "
                    "position is not protected",
                    replacement,
                )
            return ExecutionResult(
                "HOLD",
                True,
                "Terminal exchange protection reconciled; remaining position re-protected",
                replacement,
            )

        # Ratchet protection only after checking the existing stop. This prevents
        # a price gap from being mistaken for a new high and guarantees stops are
        # never loosened, even across restarts.
        if reason is None and self.settings.enable_trailing_stop:
            entry = float(trade["entry_price"])
            previous_high = float(trade.get("highest_price") or entry)
            high_water = max(previous_high, price)
            candidate_stop = original_stop
            if high_water >= entry * (1 + self.settings.breakeven_activation_pct):
                fee_rate = self.settings.trading_fee_bps / 10000
                slip = self.settings.paper_slippage_bps / 10000
                unit_cost = entry + float(trade.get("entry_fee") or 0) / float(trade["quantity"])
                breakeven = unit_cost / ((1 - fee_rate) * (1 - slip))
                if breakeven < price:
                    candidate_stop = max(candidate_stop, breakeven)
            if high_water >= entry * (1 + self.settings.trailing_stop_activation_pct):
                candidate_stop = max(
                    candidate_stop,
                    high_water * (1 - self.settings.trailing_stop_distance_pct),
                )
            if high_water > previous_high or candidate_stop > original_stop:
                normalized_stop = await self.exchange.normalize_price(
                    self.settings.symbol, candidate_stop
                )
                protection = self.db.raise_position_stop(
                    int(trade["id"]),
                    observed_price=high_water,
                    stop_price=normalized_stop,
                )
                if protection["stop_price"] > original_stop:
                    return ExecutionResult(
                        "HOLD",
                        True,
                        "Position retained; protective stop raised",
                        protection,
                    )

        if not reason:
            if (
                self.settings.mode in {"testnet", "live"}
                and not trade.get("protective_list_client_order_id")
            ):
                protection = await self._place_exchange_protection(trade)
                return ExecutionResult(
                    "HOLD",
                    True,
                    "Open position retained; exchange protection installed",
                    protection,
                )
            return ExecutionResult("HOLD", True, "Open position retained")

        qty = float(trade["quantity"])
        if self.settings.mode == "paper":
            fill_price = self._paper_execution_price(price, "SELL")
            price_d = Decimal(str(fill_price))
            qty_d = Decimal(str(qty))
            fee_rate = Decimal(str(self.settings.trading_fee_bps)) / Decimal("10000")
            fee = price_d * qty_d * fee_rate
            quote = self._paper_quote_dec()
            base = self._paper_base_dec()
            closed = self.db.close_trade(
                int(trade["id"]),
                float(price_d),
                float(fee),
                reason,
                executed_quantity=float(qty_d),
                state_updates={
                    self._paper_quote_key: quote + (price_d * qty_d - fee),
                    self._paper_base_key: max(Decimal("0"), base - qty_d),
                },
            )
            return ExecutionResult(
                "SELL",
                True,
                "Paper position closed",
                {
                    **closed,
                    "market_price": price,
                    "fill_price": float(price_d),
                    "estimated_slippage_bps": self.settings.paper_slippage_bps,
                    "exit_fee": float(fee),
                },
            )

        if self.settings.mode == "live" and not self.settings.allow_live_trading:
            return ExecutionResult("SELL", False, "Live trading blocked: ALLOW_LIVE_TRADING=false")

        inventory_issue = await self._position_inventory_issue(trade)
        if inventory_issue is not None:
            return ExecutionResult("SELL", False, inventory_issue)

        if self.settings.mode in {"testnet", "live"}:
            protective_fill = await self._cancel_exchange_protection(trade)
            if protective_fill is not None:
                if protective_fill.get("partial_pending") and not protective_fill.get("applied"):
                    return ExecutionResult(
                        "HOLD",
                        True,
                        "Exchange protective order partially filled; remaining quantity is still protected",
                        protective_fill,
                    )
                return ExecutionResult(
                    "SELL",
                    True,
                    protective_fill["reason"],
                    protective_fill,
                )
            trade = self.db.get_open_trade(self.settings.symbol, self.settings.mode)
            if trade is None:
                return ExecutionResult(
                    "SELL",
                    True,
                    "Position was already closed by exchange protection",
                )
            qty = float(trade["quantity"])

        available = await self.exchange.asset_balance(self.settings.base_asset)
        requested_qty = min(qty, available)
        normalized_qty = await self.exchange.normalize_quantity(
            self.settings.symbol, requested_qty, price
        )
        if normalized_qty <= 0:
            return ExecutionResult("SELL", False, "Sell quantity violates Binance market filters")

        order, client_order_id, fee = await self._submit_market_order("SELL", normalized_qty)
        executed_qty = self.exchange.executed_quantity(order)
        if executed_qty <= 0:
            return ExecutionResult(
                "SELL",
                False,
                f"Binance order {client_order_id} did not report an executed quantity",
                {"order": order},
            )

        fill_price = self.exchange.weighted_fill_price(order, price)
        closed = self.db.close_trade(
            int(trade["id"]),
            fill_price,
            fee,
            reason,
            executed_quantity=executed_qty,
            client_order_id=client_order_id,
        )
        return ExecutionResult(
            "SELL",
            True,
            "Binance market sell executed",
            {
                "trade": closed,
                "client_order_id": client_order_id,
                "requested_qty": normalized_qty,
                "executed_qty": executed_qty,
                "fill_price": fill_price,
                "exit_fee_quote": fee,
                "fee_valuation": order.get("_commission_quote_valuation"),
                "commissions": self.exchange.commission_details(order),
                "order": order,
            },
        )
