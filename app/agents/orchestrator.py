import asyncio
import time
from datetime import datetime, timezone

from app.agents.learning import LearningAgent
from app.agents.llm_advisor import LLMAdvisor
from app.config import Settings
from app.exchange.binance import BinanceClient
from app.execution.broker import Broker
from app.models import RiskDecision, SignalSide, StrategySignal
from app.risk.manager import RiskManager
from app.storage.db import TradingDB
from app.strategy.ensemble import EnsembleStrategy


def interval_to_ms(interval: str) -> int:
    """Convert common Binance interval strings to milliseconds."""
    interval = interval.strip()
    if len(interval) < 2:
        raise ValueError(f"Invalid interval: {interval}")
    amount = int(interval[:-1])
    unit = interval[-1]
    factors = {
        "s": 1_000,
        "m": 60_000,
        "h": 3_600_000,
        "d": 86_400_000,
        "w": 604_800_000,
        "M": 2_592_000_000,  # Approximation used only for refresh scheduling.
    }
    if unit not in factors:
        raise ValueError(f"Unsupported interval: {interval}")
    return amount * factors[unit]


class TradingOrchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = TradingDB(settings.database_path)
        self.db.init()
        self.exchange = BinanceClient(settings)
        self.strategy = EnsembleStrategy()
        self.risk = RiskManager(settings)
        self.learning = LearningAgent(settings)
        self.llm = LLMAdvisor(settings)
        self.broker = Broker(settings, self.exchange, self.db)

        self.running = False
        self.task: asyncio.Task | None = None
        self.price_task: asyncio.Task | None = None
        self.strategy_task: asyncio.Task | None = None
        self.strategy_error: str | None = None
        self.last_cycle: dict | None = None

        self.latest_price: float | None = None
        self.latest_price_event_ms: int | None = None
        self.latest_price_received_monotonic: float | None = None
        self.latest_price_source: str | None = None

        self.cached_signal: StrategySignal | None = None
        self.cached_signal_candle_close_time: int | None = None
        self.cached_signal_price: float | None = None
        self.cached_llm_adjustment: float = 0.0
        self.cached_llm_reason: str = "Not requested"

        self.last_equity: float | None = None
        self.last_equity_update_monotonic: float | None = None

    async def _on_price_tick(self, price: float, event_time_ms: int) -> None:
        self.latest_price = price
        self.latest_price_event_ms = event_time_ms
        self.latest_price_received_monotonic = time.monotonic()
        self.latest_price_source = "websocket"

    def market_snapshot(self) -> dict:
        age_ms = None
        if self.latest_price_received_monotonic is not None:
            age_ms = max(0.0, (time.monotonic() - self.latest_price_received_monotonic) * 1000)
        websocket_connected = (
            self.settings.use_websocket_market_data
            and age_ms is not None
            and age_ms <= self.settings.market_data_stale_seconds * 1000
            and self.latest_price_source == "websocket"
        )
        return {
            "price": self.latest_price,
            "source": self.latest_price_source,
            "price_age_ms": age_ms,
            "event_time_ms": self.latest_price_event_ms,
            "websocket_connected": websocket_connected,
            "check_seconds": self.settings.cycle_seconds,
            "strategy_interval": self.settings.interval,
        }

    async def _market_price(self) -> tuple[float, str, float | None]:
        snapshot = self.market_snapshot()
        if snapshot["websocket_connected"] and snapshot["price"] is not None:
            return float(snapshot["price"]), "websocket", snapshot["price_age_ms"]

        price = await self.exchange.ticker_price(self.settings.symbol)
        self.latest_price = price
        self.latest_price_event_ms = int(time.time() * 1000)
        self.latest_price_received_monotonic = time.monotonic()
        self.latest_price_source = "rest"
        return price, "rest", 0.0

    def _strategy_refresh_due(self) -> bool:
        if self.cached_signal is None or self.cached_signal_candle_close_time is None:
            return True
        interval_ms = interval_to_ms(self.settings.interval)
        grace_ms = int(self.settings.strategy_refresh_grace_seconds * 1000)
        return int(time.time() * 1000) > self.cached_signal_candle_close_time + interval_ms + grace_ms

    async def _refresh_strategy(self, force: bool = False) -> bool:
        if not force and not self._strategy_refresh_due():
            return False

        raw = await self.exchange.klines(self.settings.symbol, self.settings.interval, 250)
        if raw.empty:
            raise RuntimeError("Binance returned no candle data")

        now_ms = int(time.time() * 1000)
        signal_data = raw
        if "close_time" in raw.columns:
            completed = raw[raw["close_time"] < now_ms]
            if len(completed) >= 60:
                signal_data = completed

        if len(signal_data) < 60:
            raise RuntimeError("Not enough completed candles for strategy calculation")

        candle_close_time = int(signal_data.iloc[-1]["close_time"]) if "close_time" in signal_data.columns else now_ms
        if not force and candle_close_time == self.cached_signal_candle_close_time:
            return False

        signal_price = float(signal_data.iloc[-1]["close"])
        open_trade = self.db.get_open_trade(self.settings.symbol)
        signal = self.strategy.evaluate(signal_data, has_position=open_trade is not None)

        llm_adjustment = 0.0
        llm_reason = "Not requested"
        if signal.side != SignalSide.HOLD:
            llm_adjustment, llm_reason = await self.llm.confidence_adjustment(signal)
            signal.confidence = max(0.0, min(0.99, signal.confidence + llm_adjustment))

        self.cached_signal = signal
        self.cached_signal_candle_close_time = candle_close_time
        self.cached_signal_price = signal_price
        self.cached_llm_adjustment = llm_adjustment
        self.cached_llm_reason = llm_reason
        return True

    async def _equity(self, price: float, force: bool = False) -> float:
        if self.settings.mode == "paper":
            self.last_equity = await self.broker.equity(price)
            self.last_equity_update_monotonic = time.monotonic()
            return self.last_equity

        now = time.monotonic()
        due = (
            self.last_equity is None
            or self.last_equity_update_monotonic is None
            or now - self.last_equity_update_monotonic >= self.settings.account_refresh_seconds
        )
        if force or due:
            self.last_equity = await self.broker.equity(price)
            self.last_equity_update_monotonic = now
        return float(self.last_equity or 0.0)

    async def _poll_strategy(self, force: bool = False) -> bool:
        # Candle/LLM requests must not hold up subsequent risk-monitor cycles.
        if self.strategy_task is None and (force or self._strategy_refresh_due()):
            self.strategy_task = asyncio.create_task(self._refresh_strategy(force=force))
        task = self.strategy_task
        if task is None or (not force and not task.done()):
            return False
        try:
            updated = await task
            self.strategy_error = None
            return updated
        except Exception as exc:
            self.strategy_error = str(exc)
            return False
        finally:
            self.strategy_task = None

    async def _cycle(self, force_strategy: bool = False) -> dict:
        price, price_source, price_age_ms = await self._market_price()
        open_trade = self.db.get_open_trade(self.settings.symbol)
        signal = self.cached_signal or StrategySignal(SignalSide.HOLD, 0.0, "Waiting for strategy data")
        execution = None
        if open_trade:
            # Protective exits precede strategy, learning, and balance requests.
            execution = await self.broker.maybe_exit(signal, price)

        strategy_updated = await self._poll_strategy(force=force_strategy)
        signal = self.cached_signal or StrategySignal(SignalSide.HOLD, 0.0, "Waiting for strategy data")
        if not open_trade and strategy_updated:
            price, price_source, price_age_ms = await self._market_price()
        if open_trade and strategy_updated and execution.action == "HOLD":
            # Apply a newly available strategy exit without waiting another cycle.
            price, price_source, price_age_ms = await self._market_price()
            execution = await self.broker.maybe_exit(signal, price)
        profile = self.learning.update(self.db.closed_trades())
        equity = await self._equity(price, force=force_strategy)
        daily_pnl = self.db.realized_pnl_today()

        risk_decision: RiskDecision | None = None

        if not open_trade:
            already_used_candle = (
                signal.side == SignalSide.BUY
                and self.cached_signal_candle_close_time is not None
                and self.db.get_state("last_entry_signal_candle") == str(self.cached_signal_candle_close_time)
            )

            if already_used_candle:
                risk_decision = RiskDecision(False, "This completed candle was already used for an entry")
            elif not strategy_updated:
                risk_decision = RiskDecision(False, "Waiting for the next completed candle before a new entry")
            else:
                risk_decision = self.risk.evaluate_entry(
                    signal=signal,
                    price=price,
                    equity=equity,
                    daily_realized_pnl=daily_pnl,
                    threshold=profile.confidence_threshold,
                    risk_multiplier=profile.risk_multiplier,
                )

            if risk_decision.allowed:
                execution = await self.broker.enter(signal, risk_decision, price)
                if (
                    execution.success
                    and execution.action == "BUY"
                    and self.cached_signal_candle_close_time is not None
                ):
                    self.db.set_state("last_entry_signal_candle", self.cached_signal_candle_close_time)

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": self.settings.mode,
            "symbol": self.settings.symbol,
            "interval": self.settings.interval,
            "signal_candle_close_time": self.cached_signal_candle_close_time,
            "strategy_updated": strategy_updated,
            "strategy_error": self.strategy_error,
            "price": price,
            "price_source": price_source,
            "price_age_ms": price_age_ms,
            "signal_price": self.cached_signal_price,
            "equity": equity,
            "daily_realized_pnl": daily_pnl,
            "market_monitor": self.market_snapshot(),
            "signal": {
                "side": signal.side.value,
                "confidence": round(signal.confidence, 4),
                "reason": signal.reason,
                "features": signal.features,
            },
            "llm": {
                "adjustment": self.cached_llm_adjustment,
                "reason": self.cached_llm_reason,
            },
            "learning": profile.__dict__,
            "risk": risk_decision.__dict__ if risk_decision else None,
            "execution": execution.__dict__ if execution else {
                "action": "NONE",
                "success": True,
                "message": "No order action this market cycle",
            },
            "open_trade": self.db.get_open_trade(self.settings.symbol),
        }
        self.last_cycle = result
        return result

    async def run_once(self) -> dict:
        return await self._cycle(force_strategy=True)

    async def _price_stream_loop(self) -> None:
        await self.exchange.stream_trade_prices(self.settings.symbol, self._on_price_tick)

    async def _loop(self):
        while self.running:
            started = time.monotonic()
            try:
                await self._cycle(force_strategy=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_cycle = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "error": str(exc),
                    "market_monitor": self.market_snapshot(),
                }
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, self.settings.cycle_seconds - elapsed))

    def start(self) -> bool:
        if self.running:
            return False
        self.running = True
        if self.settings.use_websocket_market_data:
            self.price_task = asyncio.create_task(self._price_stream_loop())
        self.task = asyncio.create_task(self._loop())
        return True

    async def stop(self) -> bool:
        if not self.running:
            return False
        self.running = False

        for task in (self.task, self.price_task, self.strategy_task):
            if task:
                task.cancel()
        for task in (self.task, self.price_task, self.strategy_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass  # A completed background request may have failed.

        self.task = None
        self.price_task = None
        self.strategy_task = None
        await self.exchange.close()
        return True
