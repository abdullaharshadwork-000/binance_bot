import asyncio
import time
import math
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
    """Convert supported Binance interval strings to milliseconds."""
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
    }
    if unit not in factors:
        raise ValueError(f"Unsupported interval: {interval}")
    return amount * factors[unit]


class TradingOrchestrator:
    def __init__(self, settings: Settings, portfolio_guard=None):
        self.settings = settings
        self.portfolio_guard = portfolio_guard
        self.db = TradingDB(settings.database_path)
        self.db.init()
        self.exchange = BinanceClient(settings)
        self.strategy = EnsembleStrategy(
            min_volume_ratio=settings.strategy_min_volume_ratio,
            max_extension_atr=settings.strategy_max_extension_atr,
        )
        self.risk = RiskManager(settings)
        self.learning = LearningAgent(settings)
        self.llm = LLMAdvisor(settings)
        self.broker = Broker(settings, self.exchange, self.db)

        self.running = False
        self.task: asyncio.Task | None = None
        self.price_task: asyncio.Task | None = None
        self.strategy_task: asyncio.Task | None = None
        self.equity_task: asyncio.Task | None = None
        self.strategy_error: str | None = None
        self.reconciliation_warning: str | None = None
        self.last_cycle: dict | None = None
        self.last_execution: dict | None = None

        self._cycle_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._manual_cycle_active = False
        self._exchange_validated = False

        self.latest_price: float | None = None
        self.latest_price_event_ms: int | None = None
        self.latest_price_received_monotonic: float | None = None
        self.latest_price_source: str | None = None

        self.cached_signal: StrategySignal | None = None
        self.cached_signal_candle_close_time: int | None = None
        self.cached_signal_price: float | None = None
        self.cached_llm_adjustment: float = 0.0
        self.cached_llm_reason: str = "Not requested"
        self.pending_strategy_result: dict | None = None

        self.last_equity: float | None = None
        self.last_equity_update_monotonic: float | None = None

    @property
    def _entry_state_key(self) -> str:
        return f"last_entry_signal_candle:{self.settings.mode}:{self.settings.symbol}"

    async def _ensure_exchange_config(self) -> None:
        if self._exchange_validated:
            return
        await self.exchange.validate_symbol_assets(
            self.settings.symbol,
            self.settings.base_asset,
            self.settings.quote_asset,
        )
        reconciliation = await self.broker.reconcile_unresolved_orders()
        unresolved = [
            item for item in reconciliation
            if not item.get("resolved", False)
        ]
        if unresolved:
            ids = ", ".join(
                str(item.get("client_order_id")) for item in unresolved
            )
            self.reconciliation_warning = (
                "Unresolved Binance order outcome(s) require review before "
                f"new orders can be submitted: {ids}"
            )
        else:
            self.reconciliation_warning = None
        self._exchange_validated = True

    async def _on_price_tick(self, price: float, event_time_ms: int) -> None:
        if not math.isfinite(price) or price <= 0:
            return
        if event_time_ms > int(time.time() * 1000) + 1000:
            return
        # Ignore buffered ticks older than the latest accepted exchange event.
        if (
            self.latest_price_event_ms is not None
            and event_time_ms < self.latest_price_event_ms
        ):
            return
        self.latest_price = price
        self.latest_price_event_ms = event_time_ms
        self.latest_price_received_monotonic = time.monotonic()
        self.latest_price_source = "websocket"

    def market_snapshot(self) -> dict:
        receive_age_ms = None
        if self.latest_price_received_monotonic is not None:
            receive_age_ms = max(
                0.0,
                (time.monotonic() - self.latest_price_received_monotonic) * 1000,
            )

        exchange_age_ms = None
        if self.latest_price_event_ms is not None:
            exchange_age_ms = max(
                0.0,
                int(time.time() * 1000) - int(self.latest_price_event_ms),
            )

        ages = [x for x in (receive_age_ms, exchange_age_ms) if x is not None]
        age_ms = max(ages) if ages else None
        stale_limit_ms = self.settings.market_data_stale_seconds * 1000
        websocket_connected = (
            self.settings.use_websocket_market_data
            and age_ms is not None
            and age_ms <= stale_limit_ms
            and self.latest_price_source == "websocket"
        )
        return {
            "price": self.latest_price,
            "source": self.latest_price_source,
            "price_age_ms": age_ms,
            "receive_age_ms": receive_age_ms,
            "exchange_age_ms": exchange_age_ms,
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
        now_ms = int(time.time() * 1000)
        self.latest_price = price
        self.latest_price_event_ms = now_ms
        self.latest_price_received_monotonic = time.monotonic()
        self.latest_price_source = "rest"
        return price, "rest", 0.0

    def _strategy_refresh_due(self) -> bool:
        if self.pending_strategy_result is not None:
            return False
        if self.cached_signal is None or self.cached_signal_candle_close_time is None:
            return True
        interval_ms = interval_to_ms(self.settings.interval)
        grace_ms = int(self.settings.strategy_refresh_grace_seconds * 1000)
        return int(time.time() * 1000) > (
            self.cached_signal_candle_close_time + interval_ms + grace_ms
        )

    async def _compute_strategy(self, force: bool = False) -> dict | None:
        raw = await self.exchange.klines(
            self.settings.symbol,
            self.settings.interval,
            250,
        )
        if raw.empty:
            raise RuntimeError("Binance returned no candle data")

        now_ms = int(time.time() * 1000)
        if "close_time" not in raw.columns:
            raise RuntimeError("Candle data is missing close_time")

        completed = raw[raw["close_time"] < now_ms].copy()
        if len(completed) < 60:
            raise RuntimeError("Not enough completed candles for strategy calculation")

        candle_close_time = int(completed.iloc[-1]["close_time"])
        if (
            not force
            and candle_close_time == self.cached_signal_candle_close_time
        ):
            return None

        signal_price = float(completed.iloc[-1]["close"])
        open_trade = self.db.get_open_trade(
            self.settings.symbol,
            self.settings.mode,
        )
        signal = self.strategy.evaluate(
            completed,
            has_position=open_trade is not None,
        )

        llm_adjustment = 0.0
        llm_reason = "Not requested"
        if signal.side != SignalSide.HOLD:
            llm_adjustment, llm_reason = await self.llm.confidence_adjustment(signal)
            signal.confidence = max(
                0.0,
                min(0.99, signal.confidence + llm_adjustment),
            )

        return {
            "signal": signal,
            "candle_close_time": candle_close_time,
            "signal_price": signal_price,
            "llm_adjustment": llm_adjustment,
            "llm_reason": llm_reason,
        }

    def _commit_strategy(self, result: dict) -> None:
        self.cached_signal = result["signal"]
        self.cached_signal_candle_close_time = int(result["candle_close_time"])
        self.cached_signal_price = float(result["signal_price"])
        self.cached_llm_adjustment = float(result["llm_adjustment"])
        self.cached_llm_reason = str(result["llm_reason"])
        if (
            self.pending_strategy_result is not None
            and int(self.pending_strategy_result["candle_close_time"])
            == self.cached_signal_candle_close_time
        ):
            self.pending_strategy_result = None

    async def _poll_strategy(self, force: bool = False) -> dict | None:
        if self.pending_strategy_result is not None:
            if self._candidate_expired(self.pending_strategy_result):
                self.pending_strategy_result = None
            else:
                return self.pending_strategy_result

        if self.strategy_task is None and (
            force or self._strategy_refresh_due()
        ):
            self.strategy_task = asyncio.create_task(
                self._compute_strategy(force=force)
            )

        task = self.strategy_task
        if task is None:
            return None

        if force:
            try:
                result = await task
                self.strategy_error = None
                if result is not None:
                    self.pending_strategy_result = result
                return self.pending_strategy_result
            except Exception as exc:
                self.strategy_error = str(exc)
                return None
            finally:
                self.strategy_task = None

        if not task.done():
            return None

        try:
            result = await task
            self.strategy_error = None
            if result is not None:
                self.pending_strategy_result = result
            return self.pending_strategy_result
        except Exception as exc:
            self.strategy_error = str(exc)
            return None
        finally:
            self.strategy_task = None

    async def _poll_equity(self, price: float) -> float | None:
        if self.portfolio_guard is not None:
            self.last_equity = await self.portfolio_guard.poll_equity()
            return self.last_equity
        if self.settings.mode == "paper":
            self.last_equity = await self.broker.equity(price)
            self.last_equity_update_monotonic = time.monotonic()
            return self.last_equity

        if self.equity_task is not None and self.equity_task.done():
            task = self.equity_task
            self.equity_task = None
            try:
                self.last_equity = float(await task)
                self.last_equity_update_monotonic = time.monotonic()
            except Exception:
                # Keep the last known equity. Entry will remain blocked if none exists.
                pass

        now = time.monotonic()
        due = (
            self.last_equity is None
            or self.last_equity_update_monotonic is None
            or now - self.last_equity_update_monotonic
            >= self.settings.account_refresh_seconds
        )
        if due and self.equity_task is None:
            self.equity_task = asyncio.create_task(self.broker.equity(price))

        if self.last_equity_update_monotonic is None or time.monotonic() - self.last_equity_update_monotonic > self.settings.account_max_age_seconds:
            return None
        return self.last_equity

    def _candidate_expired(self, candidate: dict) -> bool:
        age = int(time.time() * 1000) - int(candidate["candle_close_time"])
        return age > interval_to_ms(self.settings.interval) + int(self.settings.strategy_refresh_grace_seconds * 1000)

    def _entry_quality_reason(self, candidate: dict, price: float) -> str | None:
        if self._candidate_expired(candidate):
            return "Strategy candle expired; waiting for fresh analysis"
        reference = float(candidate["signal_price"])
        if not math.isfinite(reference) or reference <= 0 or not math.isfinite(price) or price <= 0:
            return "Invalid entry price"
        if abs(price / reference - 1) > self.settings.max_entry_deviation_pct:
            return "Price moved too far from the strategy candle"
        closed = self.db.closed_trades(limit=1, mode=self.settings.mode, symbol=self.settings.symbol)
        if closed:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(closed[0]["closed_at"])).total_seconds()
            if elapsed < self.settings.entry_cooldown_seconds:
                return "Post-exit cooldown is active"
        return None

    async def _cycle_unlocked(self, force_strategy: bool = False) -> dict:
        await self._ensure_exchange_config()

        price, price_source, price_age_ms = await self._market_price()
        open_trade = self.db.get_open_trade(
            self.settings.symbol,
            self.settings.mode,
        )
        signal = self.cached_signal or StrategySignal(
            SignalSide.HOLD,
            0.0,
            "Waiting for strategy data",
        )
        execution = None

        if open_trade:
            # Protective exits are always checked before slower strategy/account work.
            execution = await self.broker.maybe_exit(signal, price)
            if execution.action == "SELL" and self.portfolio_guard is not None:
                self.portfolio_guard.invalidate_account()
            open_trade = self.db.get_open_trade(
                self.settings.symbol,
                self.settings.mode,
            )

        candidate = await self._poll_strategy(force=force_strategy)
        if candidate is not None and self._candidate_expired(candidate):
            self.pending_strategy_result = None
            self.strategy_error = "Strategy result expired before it could be used"
            candidate = None
        strategy_updated = candidate is not None
        candidate_signal = candidate["signal"] if candidate else signal

        if open_trade and candidate and (
            execution is None or execution.action == "HOLD"
        ):
            price, price_source, price_age_ms = await self._market_price()
            execution = await self.broker.maybe_exit(candidate_signal, price)
            if execution.action == "SELL" and self.portfolio_guard is not None:
                self.portfolio_guard.invalidate_account()
            open_trade = self.db.get_open_trade(
                self.settings.symbol,
                self.settings.mode,
            )

        profile = self.learning.update(
            self.db.closed_trades(
                mode=self.settings.mode,
                symbol=self.settings.symbol,
            )
        )
        equity = await self._poll_equity(price)
        daily_pnl = (
            self.portfolio_guard.realized_pnl_today()
            if self.portfolio_guard is not None
            else self.db.realized_pnl_today(
                mode=self.settings.mode,
                symbol=self.settings.symbol,
            )
        )

        risk_decision: RiskDecision | None = None
        downstream_ok = True

        if not open_trade:
            # A manual refresh/advisor can take time. Reprice before sizing.
            if candidate is not None:
                price, price_source, price_age_ms = await self._market_price()
            quality_reason = self._entry_quality_reason(candidate, price) if candidate is not None else None
            candle_close_time = (
                int(candidate["candle_close_time"])
                if candidate is not None
                else self.cached_signal_candle_close_time
            )
            already_used_candle = (
                candidate_signal.side == SignalSide.BUY
                and candle_close_time is not None
                and self.db.get_state(self._entry_state_key)
                == str(candle_close_time)
            )

            if already_used_candle:
                risk_decision = RiskDecision(
                    False,
                    "This completed candle was already used for an entry",
                )
            elif candidate is None:
                risk_decision = RiskDecision(
                    False,
                    "Waiting for the next completed candle before a new entry",
                )
            elif quality_reason is not None:
                risk_decision = RiskDecision(False, quality_reason)
            elif equity is None:
                downstream_ok = False
                risk_decision = RiskDecision(
                    False,
                    "Account equity refresh is pending; entry remains blocked",
                )
            elif (
                self.portfolio_guard is not None
                and self.portfolio_guard.entry_capacity_reason() is not None
            ):
                risk_decision = RiskDecision(
                    False,
                    self.portfolio_guard.entry_capacity_reason(),
                )
            else:
                risk_decision = self.risk.evaluate_entry(
                    signal=candidate_signal,
                    price=price,
                    equity=equity,
                    daily_realized_pnl=daily_pnl,
                    threshold=max(
                        self.settings.min_signal_confidence,
                        profile.confidence_threshold,
                    ),
                    risk_multiplier=profile.risk_multiplier,
                )

            if risk_decision.allowed:
                try:
                    if self.portfolio_guard is not None:
                        execution = await self.portfolio_guard.execute_entry(
                            self.settings.symbol,
                            lambda: self.broker.enter(
                                candidate_signal,
                                risk_decision,
                                price,
                            ),
                            notional=risk_decision.quantity * price,
                            expected_price=price,
                        )
                    else:
                        execution = await self.broker.enter(
                            candidate_signal,
                            risk_decision,
                            price,
                        )
                except Exception:
                    downstream_ok = False
                    raise

                if execution.success and execution.action == "BUY":
                    if candle_close_time is not None:
                        self.db.set_state(
                            self._entry_state_key,
                            candle_close_time,
                        )
                elif not execution.success:
                    downstream_ok = False
                    risk_decision = RiskDecision(False, execution.message)

        if candidate is not None and downstream_ok:
            self._commit_strategy(candidate)
            signal = candidate_signal
        else:
            signal = self.cached_signal or candidate_signal

        if execution is not None and execution.action in {"BUY", "SELL"}:
            self.last_execution = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **execution.__dict__,
            }

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": self.settings.mode,
            "symbol": self.settings.symbol,
            "interval": self.settings.interval,
            "signal_candle_close_time": self.cached_signal_candle_close_time,
            "strategy_updated": strategy_updated and downstream_ok,
            "strategy_pending": self.pending_strategy_result is not None,
            "strategy_error": self.strategy_error,
            "reconciliation_warning": self.reconciliation_warning,
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
            "open_trade": self.db.get_open_trade(
                self.settings.symbol,
                self.settings.mode,
            ),
        }
        self.last_cycle = result
        return result

    async def _cycle(self, force_strategy: bool = False) -> dict:
        async with self._cycle_lock:
            return await self._cycle_unlocked(force_strategy=force_strategy)

    async def analyze_once(self) -> dict:
        """Run a read-only market analysis without submitting or closing orders."""
        async with self._control_lock:
            if self.running:
                raise RuntimeError(
                    "Read-only analysis is disabled while the automated bot is running"
                )
            if self._manual_cycle_active:
                raise RuntimeError("Another manual operation is already running")
            self._manual_cycle_active = True

        try:
            await self._ensure_exchange_config()
            price, price_source, price_age_ms = await self._market_price()
            candidate = await self._compute_strategy(force=True)
            if candidate is None:
                raise RuntimeError("No completed candle was available for analysis")

            signal = candidate["signal"]
            profile = self.learning.load()
            open_trade = self.db.get_open_trade(
                self.settings.symbol,
                self.settings.mode,
            )
            equity = await self.broker.equity(price)
            daily_pnl = (
                self.portfolio_guard.realized_pnl_today()
                if self.portfolio_guard is not None
                else self.db.realized_pnl_today(
                    mode=self.settings.mode,
                    symbol=self.settings.symbol,
                )
            )

            risk_decision: RiskDecision | None = None
            if open_trade is None:
                risk_decision = self.risk.evaluate_entry(
                    signal=signal,
                    price=price,
                    equity=equity,
                    daily_realized_pnl=daily_pnl,
                    threshold=max(
                        self.settings.min_signal_confidence,
                        profile.confidence_threshold,
                    ),
                    risk_multiplier=profile.risk_multiplier,
                )

            result = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mode": self.settings.mode,
                "symbol": self.settings.symbol,
                "interval": self.settings.interval,
                "read_only": True,
                "signal_candle_close_time": int(candidate["candle_close_time"]),
                "strategy_updated": True,
                "strategy_pending": False,
                "strategy_error": None,
                "reconciliation_warning": self.reconciliation_warning,
                "price": price,
                "price_source": price_source,
                "price_age_ms": price_age_ms,
                "signal_price": float(candidate["signal_price"]),
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
                    "adjustment": float(candidate["llm_adjustment"]),
                    "reason": str(candidate["llm_reason"]),
                },
                "learning": profile.__dict__,
                "risk": risk_decision.__dict__ if risk_decision else None,
                "execution": {
                    "action": "NONE",
                    "success": True,
                    "message": "Read-only analysis; no order was submitted",
                },
                "open_trade": open_trade,
            }
            self.last_cycle = result
            return result
        finally:
            async with self._control_lock:
                self._manual_cycle_active = False

    async def run_once(self) -> dict:
        async with self._control_lock:
            if self.running:
                raise RuntimeError(
                    "Manual Run Once is disabled while the automated bot is running"
                )
            if self._manual_cycle_active:
                raise RuntimeError("A manual cycle is already running")
            self._manual_cycle_active = True

        try:
            return await self._cycle(force_strategy=True)
        finally:
            async with self._control_lock:
                self._manual_cycle_active = False

    async def _price_stream_loop(self) -> None:
        await self.exchange.stream_trade_prices(
            self.settings.symbol,
            self._on_price_tick,
        )

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
            await asyncio.sleep(
                max(0.0, self.settings.cycle_seconds - elapsed)
            )

    async def start(self) -> bool:
        async with self._control_lock:
            if self.running:
                return False
            if self._manual_cycle_active:
                raise RuntimeError(
                    "Cannot start while a manual cycle is running"
                )
            await self._ensure_exchange_config()
            self.running = True
            if self.settings.use_websocket_market_data:
                self.price_task = asyncio.create_task(
                    self._price_stream_loop()
                )
            self.task = asyncio.create_task(self._loop())
            return True

    async def stop(self) -> bool:
        async with self._control_lock:
            if not self.running:
                return False
            self.running = False

            tasks = [
                self.task,
                self.price_task,
                self.strategy_task,
                self.equity_task,
            ]
            for task in tasks:
                if task:
                    task.cancel()
            for task in tasks:
                if task:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass

            self.task = None
            self.price_task = None
            self.strategy_task = None
            self.equity_task = None
            await self.exchange.close()
            return True
