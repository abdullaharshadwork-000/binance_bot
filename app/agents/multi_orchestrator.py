import asyncio
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
    ) -> ExecutionResult:
        # Serialize the final portfolio-capacity check and order submission so two
        # symbols cannot both pass the position-count limit at the same instant.
        async with self._entry_lock:
            capacity_reason = self.entry_capacity_reason()
            if capacity_reason is not None:
                return ExecutionResult("BUY", False, capacity_reason)
            return await submit()


class MultiSymbolTradingManager:
    """Run one isolated strategy/orchestrator per configured symbol."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.portfolio = PortfolioCoordinator(settings)
        self.bots: dict[str, TradingOrchestrator] = {}

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
                portfolio_guard=self.portfolio if len(symbols) > 1 else None,
            )

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
        symbols = list(self.bots)
        results = await asyncio.gather(
            *(self.bots[symbol].stop() for symbol in symbols),
            return_exceptions=True,
        )
        output: dict[str, bool] = {}
        for symbol, result in zip(symbols, results):
            output[symbol] = False if isinstance(result, Exception) else bool(result)
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
