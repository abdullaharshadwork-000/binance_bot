import asyncio
import time
import math
from pathlib import Path
from typing import Awaitable, Callable

from app.agents.orchestrator import TradingOrchestrator
from app.config import Settings
from app.models import ExecutionResult
from app.storage.db import TradingDB


class PortfolioCoordinator:
    """Shared portfolio guard for multi-symbol entry decisions."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.symbols = settings.trading_symbols
        self.db = TradingDB(settings.database_path)
        self.db.init()
        self._entry_lock = asyncio.Lock()
        self.bots: dict[str, TradingOrchestrator] = {}
        self._balances: dict | None = None
        self._balance_task: asyncio.Task | None = None
        self._balance_time: float | None = None
        self._balance_requested_at: float | None = None
        self.account_error: str | None = None
        self._retired_tasks: set[asyncio.Task] = set()

    def _retire(self, task: asyncio.Task) -> None:
        self._retired_tasks.add(task)
        task.cancel()

        def consume(completed):
            self._retired_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(consume)

    def invalidate_account(self) -> None:
        self._balances = None
        self._balance_time = None
        self._balance_requested_at = None
        if self._balance_task is not None:
            self._retire(self._balance_task)
            self._balance_task = None

    async def close(self) -> None:
        task = self._balance_task
        self.invalidate_account()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if self._retired_tasks:
            await asyncio.gather(*list(self._retired_tasks), return_exceptions=True)

    async def poll_equity(self) -> float | None:
        if self.settings.mode != "paper":
            if self._balance_task is not None and self._balance_task.done():
                task, self._balance_task = self._balance_task, None
                try:
                    self._balances = await task
                    self._balance_time = self._balance_requested_at
                    self.account_error = None
                except Exception as exc:
                    self.account_error = str(exc)
                    self._balances = None
            due = self._balance_requested_at is None or time.monotonic() - self._balance_requested_at >= self.settings.account_refresh_seconds
            if due and self._balance_task is None and self.bots:
                # Age from request start, so a slow response cannot look brand new.
                self._balance_requested_at = time.monotonic()
                self._balance_task = asyncio.create_task(next(iter(self.bots.values())).exchange.account())
        return self.snapshot()["equity"]

    def snapshot(self) -> dict:
        equity = None
        cash = None
        exposure = 0.0
        error = self.account_error
        age = None if self._balance_time is None else time.monotonic() - self._balance_time
        holdings = {}
        if self.settings.mode == "paper":
            key = f"paper_balance:{self.settings.mode}:quote:{self.settings.quote_asset}"
            cash = float(self.db.get_state(key, str(self.settings.paper_starting_balance)))
            for symbol in self.symbols:
                key = f"paper_balance:{self.settings.mode}:base:{symbol}"
                holdings[symbol] = float(self.db.get_state(key, "0"))
            equity = cash
        elif self._balances is not None and age is not None and age <= self.settings.account_max_age_seconds:
            balances = {item["asset"]: item for item in self._balances.get("balances", [])}
            quote = balances.get(self.settings.quote_asset, {})
            cash = float(quote.get("free", 0))
            equity = cash + float(quote.get("locked", 0))
            for symbol in self.symbols:
                asset = balances.get(self.settings.base_for_symbol(symbol), {})
                holdings[symbol] = float(asset.get("free", 0)) + float(asset.get("locked", 0))
        else:
            error = error or "Portfolio account snapshot is pending or stale"
        if equity is not None:
            for symbol, quantity in holdings.items():
                if quantity <= 0:
                    continue
                bot = self.bots.get(symbol)
                market = bot.market_snapshot() if bot else {}
                price = market.get("price")
                price_age = market.get("price_age_ms")
                if price is None or price_age is None or price_age > self.settings.market_data_stale_seconds * 1000 or not math.isfinite(price) or price <= 0:
                    equity = None
                    error = f"Fresh valuation required for held asset {symbol}"
                    break
                exposure += quantity * price
            if equity is not None:
                equity += exposure
                if not math.isfinite(equity) or equity <= 0:
                    equity = None
                    error = "Portfolio equity is invalid"
        return {"equity": equity, "available_quote": cash, "exposure": exposure,
                "exposure_fraction": exposure / equity if equity else None,
                "max_exposure_fraction": self.settings.max_portfolio_exposure_fraction,
                "account_age_seconds": age, "error": error}

    def realized_pnl_today(self) -> float:
        return self.db.realized_pnl_today_portfolio(
            mode=self.settings.mode,
            symbols=self.symbols,
        )

    def entry_capacity_reason(self) -> str | None:
        open_positions = self.db.count_open_trades(
            mode=self.settings.mode,
            symbols=self.symbols,
        )
        if open_positions >= self.settings.max_concurrent_positions:
            return (
                "Portfolio position limit reached "
                f"({open_positions}/{self.settings.max_concurrent_positions})"
            )
        return None

    async def execute_entry(
        self,
        symbol: str,
        submit: Callable[[], Awaitable[ExecutionResult]],
        *,
        notional: float | None = None,
        expected_price: float | None = None,
    ) -> ExecutionResult:
        # Serialize the final portfolio-capacity check and order submission so two
        # symbols cannot both pass the position-count limit at the same instant.
        async with self._entry_lock:
            if symbol not in self.symbols:
                return ExecutionResult("BUY", False, "Symbol is outside the configured trading list")
            capacity_reason = self.entry_capacity_reason()
            if capacity_reason is not None:
                return ExecutionResult("BUY", False, capacity_reason)
            if notional is not None:
                for configured_symbol in self.symbols:
                    if self.db.has_unresolved_order(mode=self.settings.mode, symbol=configured_symbol):
                        return ExecutionResult("BUY", False, f"Unresolved order on {configured_symbol}; portfolio entries blocked")
                if expected_price is not None:
                    market = self.bots[symbol].market_snapshot()
                    current = market.get("price")
                    age = market.get("price_age_ms")
                    if current is None or not math.isfinite(current) or current <= 0 or not math.isfinite(expected_price) or expected_price <= 0 or age is None or age > self.settings.market_data_stale_seconds * 1000:
                        return ExecutionResult("BUY", False, "Entry price became stale while waiting for portfolio approval")
                    if abs(current / expected_price - 1) > 0.001:
                        return ExecutionResult("BUY", False, "Entry price changed; resize on the next cycle")
                snapshot = self.snapshot()
                equity = snapshot["equity"]
                if equity is None:
                    return ExecutionResult("BUY", False, snapshot["error"] or "Portfolio valuation unavailable")
                costs = 1 + (self.settings.trading_fee_bps + self.settings.paper_slippage_bps) / 10000
                if not math.isfinite(notional) or notional <= 0:
                    return ExecutionResult("BUY", False, "Invalid entry notional")
                if notional * costs > snapshot["available_quote"]:
                    return ExecutionResult("BUY", False, "Insufficient shared quote balance including costs")
                if snapshot["exposure"] + notional * costs > equity * self.settings.max_portfolio_exposure_fraction:
                    return ExecutionResult("BUY", False, "Portfolio exposure limit reached")
                if self.realized_pnl_today() <= -equity * self.settings.max_daily_loss_fraction:
                    return ExecutionResult("BUY", False, "Portfolio daily loss limit reached")
            try:
                return await submit()
            finally:
                # A timeout can still mean the order executed. Require a new
                # account snapshot before any following non-paper allocation.
                if self.settings.mode != "paper":
                    self.invalidate_account()


class MultiSymbolTradingManager:
    """Run one isolated strategy/orchestrator per configured symbol."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.portfolio = PortfolioCoordinator(settings)
        self.bots: dict[str, TradingOrchestrator] = {}
        self._control_lock = asyncio.Lock()

        symbols = settings.trading_symbols
        profile_path = Path(settings.learning_profile_path)

        for symbol in symbols:
            base_asset = settings.base_for_symbol(symbol)
            if base_asset is None:
                raise ValueError(
                    f"Could not derive base asset for {symbol} using quote asset "
                    f"{settings.quote_asset}"
                )

            if len(symbols) > 1:
                child_profile = str(
                    profile_path.with_name(
                        f"{profile_path.stem}_{symbol}{profile_path.suffix}"
                    )
                )
            else:
                child_profile = settings.learning_profile_path

            child_settings = settings.model_copy(
                update={
                    "symbol": symbol,
                    "base_asset": base_asset,
                    "symbols": symbol,
                    "learning_profile_path": child_profile,
                }
            )
            self.bots[symbol] = TradingOrchestrator(
                child_settings,
                portfolio_guard=self.portfolio,
            )
        self.portfolio.bots = self.bots

    @property
    def symbols(self) -> list[str]:
        return list(self.bots)

    @property
    def primary_bot(self) -> TradingOrchestrator:
        return self.bots[self.symbols[0]]

    @property
    def running(self) -> bool:
        return any(bot.running for bot in self.bots.values())

    def get_bot(self, symbol: str | None = None) -> TradingOrchestrator:
        selected = (symbol or self.symbols[0]).strip().upper()
        try:
            return self.bots[selected]
        except KeyError as exc:
            raise ValueError(
                f"Unknown configured symbol {selected}. "
                f"Available: {', '.join(self.symbols)}"
            ) from exc

    async def start(self) -> dict[str, bool]:
        async with self._control_lock:
            return await self._start_unlocked()

    async def _start_unlocked(self) -> dict[str, bool]:
        started: dict[str, bool] = {}
        started_bots: list[TradingOrchestrator] = []
        try:
            for symbol, bot in self.bots.items():
                did_start = await bot.start()
                started[symbol] = did_start
                if did_start:
                    started_bots.append(bot)
            return started
        except Exception:
            for bot in reversed(started_bots):
                try:
                    await bot.stop()
                except Exception:
                    pass
            raise

    async def stop(self) -> dict[str, bool]:
        async with self._control_lock:
            return await self._stop_unlocked()

    async def _stop_unlocked(self) -> dict[str, bool]:
        symbols = list(self.bots)
        results = await asyncio.gather(
            *(self.bots[symbol].stop() for symbol in symbols),
            return_exceptions=True,
        )
        output: dict[str, bool] = {}
        for symbol, result in zip(symbols, results):
            output[symbol] = False if isinstance(result, Exception) else bool(result)
        await self.portfolio.close()
        return output

    async def analyze_all(self) -> dict[str, dict]:
        symbols = list(self.bots)
        results = await asyncio.gather(
            *(self.bots[symbol].analyze_once() for symbol in symbols),
            return_exceptions=True,
        )
        output: dict[str, dict] = {}
        for symbol, result in zip(symbols, results):
            if isinstance(result, Exception):
                output[symbol] = {"error": str(result)}
            else:
                output[symbol] = result
        return output

    def overview(self) -> list[dict]:
        rows = []
        for symbol, bot in self.bots.items():
            cycle = bot.last_cycle or {}
            signal = cycle.get("signal") or {}
            execution = bot.last_execution or {}
            open_trade = bot.db.get_open_trade(symbol, bot.settings.mode)
            rows.append(
                {
                    "symbol": symbol,
                    "base_asset": bot.settings.base_asset,
                    "quote_asset": bot.settings.quote_asset,
                    "running": bot.running,
                    "price": bot.latest_price,
                    "signal": signal.get("side", "WAITING"),
                    "confidence": signal.get("confidence"),
                    "risk_allowed": (cycle.get("risk") or {}).get("allowed"),
                    "risk_reason": (cycle.get("risk") or {}).get("reason"),
                    "open_position": open_trade is not None,
                    "last_execution": execution.get("action"),
                    "error": (
                        cycle.get("error")
                        or cycle.get("strategy_error")
                        or cycle.get("reconciliation_warning")
                    ),
                }
            )
        return rows
