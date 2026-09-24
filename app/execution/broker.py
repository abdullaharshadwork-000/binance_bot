import uuid
from decimal import Decimal

from app.config import Settings
from app.exchange.binance import BinanceClient
from app.models import ExecutionResult, RiskDecision, SignalSide, StrategySignal
from app.storage.db import TradingDB


class Broker:
    def __init__(self, settings: Settings, exchange: BinanceClient, db: TradingDB):
        self.settings = settings
        self.exchange = exchange
        self.db = db
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

    async def _submit_market_order(self, side: str, qty: float) -> tuple[dict, str]:
        client_order_id = self._new_client_order_id(side)
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
                raise RuntimeError(
                    "Order outcome is uncertain. Automatic re-submission was blocked; "
                    f"manual/restart reconciliation is required. Submit error: {submit_error}; "
                    f"reconcile error: {reconcile_error}"
                ) from submit_error

        executed_qty = self.exchange.executed_quantity(order)
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
        return order, client_order_id

    async def reconcile_unresolved_orders(self) -> list[dict]:
        """Re-check uncertain exchange orders without ever blindly resubmitting them."""
        if self.settings.mode == "paper":
            return []

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
            fill_price = (
                self.exchange.weighted_fill_price(order, 0.0)
                if executed_qty > 0 else None
            )

            if executed_qty > 0 and record["status"] in {
                "PENDING_SUBMIT", "UNKNOWN", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"
            }:
                # We intentionally do not invent/reconstruct a position from incomplete
                # order history. Block further trading until a human reviews it.
                stored_status = "RECOVERY_REQUIRED"
            else:
                stored_status = raw_status

            self.db.update_order_record(
                client_order_id=client_order_id,
                binance_order_id=(
                    str(order.get("orderId"))
                    if order.get("orderId") is not None else None
                ),
                executed_quantity=executed_qty,
                average_fill_price=fill_price,
                status=stored_status,
                commission_quote=float(record.get("commission_quote") or 0.0),
                commission_details=[],
            )
            results.append({
                "client_order_id": client_order_id,
                "status": stored_status,
                "resolved": stored_status not in {
                    "PENDING_SUBMIT", "UNKNOWN", "NEW", "PARTIALLY_FILLED",
                    "PENDING_CANCEL", "RECOVERY_REQUIRED",
                },
                "executed_quantity": executed_qty,
            })
        return results

    @staticmethod
    def _base_commission(order: dict, base_asset: str) -> float:
        total = Decimal("0")
        for fill in order.get("fills") or []:
            if str(fill.get("commissionAsset") or "") == base_asset:
                total += Decimal(str(fill.get("commission") or "0"))
        return float(total)

    async def enter(self, signal: StrategySignal, risk: RiskDecision, price: float) -> ExecutionResult:
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
        if qty <= 0:
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

        order, client_order_id = await self._submit_market_order("BUY", qty)
        executed_qty = self.exchange.executed_quantity(order)
        if executed_qty <= 0:
            return ExecutionResult(
                "BUY",
                False,
                f"Binance order {client_order_id} did not report an executed quantity",
                {"order": order},
            )

        fill_price = self.exchange.weighted_fill_price(order, price)
        fee = await self.exchange.order_fee_quote(order, fill_price)
        base_commission = self._base_commission(order, self.settings.base_asset)
        held_qty = max(0.0, executed_qty - base_commission)
        if held_qty <= 0:
            return ExecutionResult("BUY", False, "Executed buy left no usable base-asset quantity")

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
                "commissions": self.exchange.commission_details(order),
                "order": order,
            },
        )

    async def maybe_exit(self, signal: StrategySignal, price: float) -> ExecutionResult:
        trade = self.db.get_open_trade(self.settings.symbol, self.settings.mode)
        if not trade:
            return ExecutionResult("HOLD", True, "No open position")
        if self.db.has_unresolved_order(mode=self.settings.mode, symbol=self.settings.symbol):
            return ExecutionResult(
                "HOLD",
                False,
                "An earlier Binance order has an unresolved outcome; duplicate exit submission is blocked",
            )

        reason = None
        original_stop = float(trade["stop_price"])
        if price <= original_stop:
            reason = "Stop loss"
        elif price >= float(trade["take_profit_price"]):
            reason = "Take profit"
        elif signal.side == SignalSide.SELL:
            reason = signal.reason

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

        available = await self.exchange.asset_balance(self.settings.base_asset)
        requested_qty = min(qty, available)
        normalized_qty = await self.exchange.normalize_quantity(
            self.settings.symbol, requested_qty, price
        )
        if normalized_qty <= 0:
            return ExecutionResult("SELL", False, "Sell quantity violates Binance market filters")

        order, client_order_id = await self._submit_market_order("SELL", normalized_qty)
        executed_qty = self.exchange.executed_quantity(order)
        if executed_qty <= 0:
            return ExecutionResult(
                "SELL",
                False,
                f"Binance order {client_order_id} did not report an executed quantity",
                {"order": order},
            )

        fill_price = self.exchange.weighted_fill_price(order, price)
        fee = await self.exchange.order_fee_quote(order, fill_price)
        closed = self.db.close_trade(
            int(trade["id"]),
            fill_price,
            fee,
            reason,
            executed_quantity=executed_qty,
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
                "commissions": self.exchange.commission_details(order),
                "order": order,
            },
        )
