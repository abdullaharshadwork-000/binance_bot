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

    def _init_paper_balances(self):
        if self.db.get_state("paper_quote_balance") is None:
            self.db.set_state("paper_quote_balance", self.settings.paper_starting_balance)
        if self.db.get_state("paper_base_balance") is None:
            self.db.set_state("paper_base_balance", 0.0)

    def _paper_quote_dec(self) -> Decimal:
        return Decimal(str(self.db.get_state("paper_quote_balance", "0") or "0"))

    def _paper_base_dec(self) -> Decimal:
        return Decimal(str(self.db.get_state("paper_base_balance", "0") or "0"))

    def _paper_quote(self) -> float:
        return float(self._paper_quote_dec())

    def _paper_base(self) -> float:
        return float(self._paper_base_dec())

    async def equity(self, price: float) -> float:
        price_d = Decimal(str(price))
        if self.settings.mode == "paper":
            return float(self._paper_quote_dec() + self._paper_base_dec() * price_d)

        balances = await self.exchange.asset_balances({self.settings.quote_asset, self.settings.base_asset})
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

    async def enter(self, signal: StrategySignal, risk: RiskDecision, price: float) -> ExecutionResult:
        if not risk.allowed or risk.quantity <= 0:
            return ExecutionResult("BUY", False, risk.reason)
        if self.db.get_open_trade(self.settings.symbol):
            return ExecutionResult("BUY", False, "Position already open")

        qty = await self.exchange.normalize_quantity(self.settings.symbol, risk.quantity, price)
        if qty <= 0:
            return ExecutionResult("BUY", False, "Quantity is below Binance symbol minimums")

        if self.settings.mode == "paper":
            fill_price = self._paper_execution_price(price, "BUY")
            price_d = Decimal(str(fill_price))
            qty_d = Decimal(str(qty))
            fee_rate = Decimal(str(self.settings.trading_fee_bps)) / Decimal("10000")
            fee = price_d * qty_d * fee_rate
            cost = price_d * qty_d + fee
            quote = self._paper_quote_dec()
            if cost > quote:
                return ExecutionResult("BUY", False, "Insufficient paper quote balance")

            stop_price, take_profit_price = await self._risk_prices_from_fill(fill_price)
            self.db.set_state("paper_quote_balance", quote - cost)
            self.db.set_state("paper_base_balance", self._paper_base_dec() + qty_d)
            trade_id = self.db.open_trade(
                mode=self.settings.mode,
                symbol=self.settings.symbol,
                quantity=float(qty_d),
                entry_price=float(price_d),
                entry_fee=float(fee),
                reason=signal.reason,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
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

        order = await self.exchange.market_order(self.settings.symbol, "BUY", qty)
        fill_price = self.exchange.weighted_fill_price(order, price)
        fee = self.exchange.estimated_order_fee_quote(order, fill_price)
        stop_price, take_profit_price = await self._risk_prices_from_fill(fill_price)
        trade_id = self.db.open_trade(
            mode=self.settings.mode,
            symbol=self.settings.symbol,
            quantity=qty,
            entry_price=fill_price,
            entry_fee=fee,
            reason=signal.reason,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
        )
        return ExecutionResult(
            "BUY",
            True,
            "Binance market buy submitted",
            {"trade_id": trade_id, "fill_price": fill_price, "entry_fee_estimate": fee, "order": order},
        )

    async def maybe_exit(self, signal: StrategySignal, price: float) -> ExecutionResult:
        trade = self.db.get_open_trade(self.settings.symbol)
        if not trade:
            return ExecutionResult("HOLD", True, "No open position")

        reason = None
        if price <= float(trade["stop_price"]):
            reason = "Stop loss"
        elif price >= float(trade["take_profit_price"]):
            reason = "Take profit"
        elif signal.side == SignalSide.SELL:
            reason = signal.reason

        if not reason:
            return ExecutionResult("HOLD", True, "Open position retained")

        qty = float(trade["quantity"])
        if self.settings.mode == "paper":
            fill_price = self._paper_execution_price(price, "SELL")
            price_d = Decimal(str(fill_price))
            qty_d = Decimal(str(qty))
            fee_rate = Decimal(str(self.settings.trading_fee_bps)) / Decimal("10000")
            fee = price_d * qty_d * fee_rate
            self.db.set_state(
                "paper_quote_balance",
                self._paper_quote_dec() + (price_d * qty_d - fee),
            )
            self.db.set_state(
                "paper_base_balance",
                max(Decimal("0"), self._paper_base_dec() - qty_d),
            )
            closed = self.db.close_trade(int(trade["id"]), float(price_d), float(fee), reason)
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
        qty = min(qty, available)
        qty = await self.exchange.normalize_quantity(self.settings.symbol, qty, price)
        if qty <= 0:
            return ExecutionResult("SELL", False, "Sell quantity is below Binance symbol minimums")

        order = await self.exchange.market_order(self.settings.symbol, "SELL", qty)
        fill_price = self.exchange.weighted_fill_price(order, price)
        fee = self.exchange.estimated_order_fee_quote(order, fill_price)
        closed = self.db.close_trade(int(trade["id"]), fill_price, fee, reason)
        return ExecutionResult(
            "SELL",
            True,
            "Binance market sell submitted",
            {"trade": closed, "fill_price": fill_price, "exit_fee_estimate": fee, "order": order},
        )
