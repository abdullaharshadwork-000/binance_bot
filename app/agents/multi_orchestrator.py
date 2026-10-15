import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.agents.orchestrator import TradingOrchestrator
from app.config import Settings
from app.exchange.binance import BinanceClient
from app.models import ExecutionResult
from app.runtime_lock import RuntimeFileLock
from app.storage.db import TradingDB

logger = logging.getLogger(__name__)


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
        self._valuation_task: asyncio.Task | None = None
        self._other_holdings: list[dict] | None = None
        self._other_holdings_error: str | None = None
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

    def _unconfigured_account_assets(self, account: dict) -> tuple[list[dict], str | None]:
        rows = account.get("balances") if isinstance(account, dict) else None
        if not isinstance(rows, list):
            return [], "Signed account response omitted its balances list"
        configured_assets = {
            self.settings.base_for_symbol(symbol)
            for symbol in self.symbols
        }
        configured_assets.discard(None)
        configured_assets.add(self.settings.quote_asset)
        totals: dict[str, Decimal] = {}
        for row in rows:
            if not isinstance(row, dict):
                return [], "Signed account response contains an invalid balance row"
            asset = str(row.get("asset") or "").strip().upper()
            if not asset:
                return [], "Signed account balance row is missing its asset identifier"
            if asset in configured_assets:
                continue
            try:
                free = Decimal(str(row.get("free") or "0"))
                locked = Decimal(str(row.get("locked") or "0"))
                total = free + locked
            except (InvalidOperation, TypeError, ValueError):
                return [], f"Could not parse the {asset} account balance"
            if not total.is_finite() or total < 0:
                return [], f"Invalid free/locked quantity for account asset {asset}"
            if total > 0:
                totals[asset] = totals.get(asset, Decimal("0")) + total
        return [
            {"asset": asset, "quantity": float(quantity)}
            for asset, quantity in sorted(totals.items())
        ], None

    async def _value_unconfigured_assets(self, account: dict) -> tuple[list[dict], str | None]:
        assets, error = self._unconfigured_account_assets(account)
        if error or not assets:
            return [], error
        if not self.bots:
            return [], "No exchange client is available to value other account assets"
        exchange = next(iter(self.bots.values())).exchange
        try:
            pairs = await exchange.asset_quote_conversion_pairs(
                [item["asset"] for item in assets],
                self.settings.quote_asset,
            )
            missing = [item["asset"] for item in assets if item["asset"] not in pairs]
            if missing:
                return [], (
                    "Cannot value non-universe account asset(s) without an active "
                    f"Spot pair against {self.settings.quote_asset}: {', '.join(missing)}"
                )
            symbols = list(dict.fromkeys(pair["symbol"] for pair in pairs.values()))
            prices = await exchange.ticker_prices(symbols)
            values = []
            for item in assets:
                asset = item["asset"]
                pair = pairs[asset]
                price = prices.get(pair["symbol"])
                if price is None or not math.isfinite(price) or price <= 0:
                    return [], f"Fresh quote price unavailable for unconfigured account asset {asset}"
                quantity = item["quantity"]
                value = quantity / price if pair["inverse"] else quantity * price
                if not math.isfinite(value) or value < 0:
                    return [], f"Invalid quote valuation for unconfigured account asset {asset}"
                values.append({
                    "asset": asset,
                    "symbol": pair["symbol"],
                    "quantity": quantity,
                    "price": price,
                    "value": value,
                })
            return values, None
        except Exception as exc:
            return [], f"Could not value non-universe account assets: {exc}"

    def invalidate_account(self) -> None:
        self._balances = None
        self._balance_time = None
        self._balance_requested_at = None
        self._other_holdings = None
        self._other_holdings_error = None
        if self._balance_task is not None:
            self._retire(self._balance_task)
            self._balance_task = None
        if self._valuation_task is not None:
            self._retire(self._valuation_task)
            self._valuation_task = None

    async def close(self) -> None:
        tasks = [task for task in (self._balance_task, self._valuation_task) if task is not None]
        self.invalidate_account()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._retired_tasks:
            await asyncio.gather(*list(self._retired_tasks), return_exceptions=True)

    async def poll_equity(self) -> float | None:
        if self.settings.mode != "paper":
            if self._valuation_task is not None and self._valuation_task.done():
                task, self._valuation_task = self._valuation_task, None
                try:
                    self._other_holdings, self._other_holdings_error = task.result()
                except Exception as exc:
                    self._other_holdings = []
                    self._other_holdings_error = f"Could not value other account assets: {exc}"
            if self._balance_task is not None and self._balance_task.done():
                task, self._balance_task = self._balance_task, None
                try:
                    self._balances = await task
                    self._balance_time = self._balance_requested_at
                    self.account_error = None
                    assets, error = self._unconfigured_account_assets(self._balances)
                    self._other_holdings_error = error
                    if error:
                        self._other_holdings = []
                    elif assets:
                        self._other_holdings = None
                        self._valuation_task = asyncio.create_task(
                            self._value_unconfigured_assets(self._balances)
                        )
                    else:
                        self._other_holdings = []
                except Exception as exc:
                    self.account_error = str(exc)
                    self._balances = None
                    self._other_holdings = None
                    self._other_holdings_error = None
            due = self._balance_requested_at is None or time.monotonic() - self._balance_requested_at >= self.settings.account_refresh_seconds
            if due and self._balance_task is None and self._valuation_task is None and self.bots:
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
        holding_details = []
        configured_assets = {
            self.settings.base_for_symbol(symbol) for symbol in self.symbols
        }
        configured_assets.discard(None)
        managed_exposure = 0.0
        unmanaged_exposure = 0.0
        if self.settings.mode == "paper":
            key = f"paper_balance:{self.settings.mode}:quote:{self.settings.quote_asset}"
            cash = float(self.db.get_state(key, str(self.settings.paper_starting_balance)))
            for symbol in self.symbols:
                key = f"paper_balance:{self.settings.mode}:base:{symbol}"
                holdings[symbol] = float(self.db.get_state(key, "0"))
            equity = cash
        elif self._balances is not None and age is not None and age <= self.settings.account_max_age_seconds:
            unconfigured_assets, balance_error = self._unconfigured_account_assets(self._balances)
            valued_assets = {item["asset"] for item in (self._other_holdings or [])}
            expected_assets = {item["asset"] for item in unconfigured_assets}
            if balance_error:
                error = balance_error
            elif self._other_holdings_error:
                error = self._other_holdings_error
            elif expected_assets and (self._other_holdings is None or expected_assets != valued_assets):
                missing = sorted(expected_assets - valued_assets)
                unexpected = sorted(valued_assets - expected_assets)
                details = []
                if missing:
                    details.append("missing " + ", ".join(missing))
                if unexpected:
                    details.append("stale " + ", ".join(unexpected))
                error = "Non-universe account valuation is incomplete: " + "; ".join(details)
            else:
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
                value = quantity * price
                trade = self.db.get_open_trade(symbol, self.settings.mode)
                managed_quantity = min(quantity, float(trade["quantity"])) if trade else 0.0
                unmanaged_quantity = max(0.0, quantity - managed_quantity)
                managed_value = managed_quantity * price
                unmanaged_value = unmanaged_quantity * price
                exposure += value
                managed_exposure += managed_value
                unmanaged_exposure += unmanaged_value
                holding_details.append({
                    "symbol": symbol, "quantity": quantity, "value": value,
                    "managed_quantity": managed_quantity,
                    "unmanaged_quantity": unmanaged_quantity,
                    "managed_value": managed_value, "unmanaged_value": unmanaged_value,
                })
            if equity is not None:
                for item in self._other_holdings or []:
                    if item["asset"] in configured_assets:
                        continue
                    value = float(item["value"])
                    if not math.isfinite(value) or value < 0:
                        equity = None
                        error = f"Invalid account valuation for {item['asset']}"
                        break
                    exposure += value
                    unmanaged_exposure += value
                    holding_details.append({
                        "symbol": item["symbol"],
                        "asset": item["asset"],
                        "quantity": item["quantity"],
                        "value": value,
                        "managed_quantity": 0.0,
                        "unmanaged_quantity": item["quantity"],
                        "managed_value": 0.0,
                        "unmanaged_value": value,
                    })
            if equity is not None:
                equity += exposure
                if not math.isfinite(equity) or equity <= 0:
                    equity = None
                    error = "Portfolio equity is invalid"
        # Partial valuations must not look like complete, current account totals.
        valuation_valid = equity is not None
        exposure_headroom = max(0.0, equity * self.settings.max_portfolio_exposure_fraction - exposure) if valuation_valid else None
        entry_block_reason = error if not valuation_valid else None
        if valuation_valid and exposure_headroom <= 0:
            entry_block_reason = (
                f"New entries blocked: account coin exposure {exposure / equity:.1%} "
                f"is at or above the {self.settings.max_portfolio_exposure_fraction:.1%} limit. "
                "Holdings outside bot positions count toward this limit."
            )
        return {"equity": equity, "available_quote": cash,
                "exposure": exposure if valuation_valid else None,
                "managed_exposure": managed_exposure if valuation_valid else None,
                "unmanaged_exposure": unmanaged_exposure if valuation_valid else None,
                "holdings": holding_details if valuation_valid else [],
                "other_account_assets": self._other_holdings if valuation_valid else [],
                "exposure_headroom": exposure_headroom,
                "entry_block_reason": entry_block_reason,
                "exposure_scope": "Selected markets plus all other valued non-quote account assets",
                "exposure_fraction": exposure / equity if equity else None,
                "max_exposure_fraction": self.settings.max_portfolio_exposure_fraction,
                "account_age_seconds": age, "error": error}

    def realized_pnl_today(self) -> float:
        return self.db.realized_pnl_today_portfolio(
            mode=self.settings.mode,
            symbols=self.symbols,
        )

    def loss_pause_reason(self) -> str | None:
        if self.settings.loss_streak_pause_seconds == 0:
            return None
        recent = self.db.recent_portfolio_closes(
            mode=self.settings.mode, symbols=self.symbols,
            limit=self.settings.max_consecutive_losses,
        )
        if len(recent) < self.settings.max_consecutive_losses or any(
            float(trade["pnl"] or 0) >= 0 for trade in recent
        ):
            return None
        closed_at = datetime.fromisoformat(recent[0]["closed_at"])
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=UTC)
        elapsed = (datetime.now(UTC) - closed_at).total_seconds()
        remaining = self.settings.loss_streak_pause_seconds - elapsed
        if remaining > 0:
            return f"Loss-streak pause: {len(recent)} consecutive losses; retry in {math.ceil(remaining)}s"
        return None

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
            pause_reason = self.loss_pause_reason()
            if pause_reason is not None:
                return ExecutionResult("BUY", False, pause_reason, {"order_submitted": False})
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
                slippage_bps = self.settings.paper_slippage_bps if self.settings.mode == "paper" else self.settings.max_entry_slippage_bps
                costs = (1 + self.settings.trading_fee_bps / 10000) * (1 + slippage_bps / 10000)
                if not math.isfinite(notional) or notional <= 0:
                    return ExecutionResult("BUY", False, "Invalid entry notional")
                if notional * costs > snapshot["available_quote"]:
                    return ExecutionResult("BUY", False, "Insufficient shared quote balance including costs")
                if snapshot["exposure"] + notional * costs > equity * self.settings.max_portfolio_exposure_fraction:
                    return ExecutionResult(
                        "BUY", False,
                        f"Portfolio exposure limit reached: account holdings {snapshot['exposure_fraction']:.1%}, "
                        f"limit {self.settings.max_portfolio_exposure_fraction:.1%}; "
                        f"holdings outside bot positions {snapshot['unmanaged_exposure']:.2f} {self.settings.quote_asset}",
                        {"order_submitted": False, "exposure": snapshot["exposure"],
                         "unmanaged_exposure": snapshot["unmanaged_exposure"],
                         "max_exposure": equity * self.settings.max_portfolio_exposure_fraction,
                         "requested_notional_with_costs": notional * costs},
                    )
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
        self._dashboard_price_lock = asyncio.Lock()
        self._runtime_lock = RuntimeFileLock(settings.database_path)
        self.universe_metadata: dict[str, dict] = {}
        self.universe_error: str | None = None
        self.universe_refreshed_at: str | None = None
        self._universe_refreshed_monotonic: float | None = None
        self._universe_last_attempt_monotonic: float | None = None
        self._build_bots(settings.trading_symbols)
        self.portfolio.bots = self.bots

    def _build_bots(self, symbols: list[str]) -> None:
        selected = list(dict.fromkeys(symbols))
        self.bots = {
            symbol: self._build_bot(symbol, len(selected))
            for symbol in selected
        }

    def _build_bot(self, symbol: str, total_symbols: int) -> TradingOrchestrator:
        base_asset = self.settings.base_for_symbol(symbol)
        if base_asset is None:
            raise ValueError(
                f"Could not derive base asset for {symbol} using quote asset "
                f"{self.settings.quote_asset}"
            )
        profile_path = Path(self.settings.learning_profile_path)
        child_profile = (
            str(profile_path.with_name(
                f"{profile_path.stem}_{symbol}{profile_path.suffix}"
            ))
            if total_symbols > 1
            else self.settings.learning_profile_path
        )
        child_settings = self.settings.model_copy(
            update={
                "symbol": symbol,
                "base_asset": base_asset,
                "symbols": symbol,
                "learning_profile_path": child_profile,
            }
        )
        return TradingOrchestrator(child_settings, portfolio_guard=self.portfolio)

    async def _refresh_universe_unlocked(self, *, force: bool = True) -> dict:
        if self.running:
            raise RuntimeError("Stop all markets before refreshing the trading universe")
        if any(bot._manual_cycle_active for bot in self.bots.values()):
            raise RuntimeError("Wait for manual analysis/order checks to finish before refreshing markets")
        if (
            not force
            and self._universe_last_attempt_monotonic is not None
            and time.monotonic() - self._universe_last_attempt_monotonic
            < self.settings.universe_refresh_minutes * 60
        ):
            return self.universe_snapshot()
        self._universe_last_attempt_monotonic = time.monotonic()

        metadata: dict[str, dict] = {}
        if self.settings.universe_mode == "top_volume":
            discovery = BinanceClient(self.settings)
            try:
                ranked = await discovery.discover_top_volume_symbols(
                    self.settings.quote_asset,
                    limit=self.settings.universe_max_symbols,
                    min_quote_volume=self.settings.universe_min_quote_volume,
                    excluded_bases=self.settings.excluded_base_assets,
                )
                metadata = {item["symbol"]: item for item in ranked}
                ordered = [item["symbol"] for item in ranked]

                # Legacy SYMBOLS and explicit pins remain included, but no longer
                # limit discovery to a fixed list.
                pins = [
                    *self.settings.configured_symbols,
                    *self.settings.pinned_trading_symbols,
                ]
                for symbol in dict.fromkeys(pins):
                    base = self.settings.base_for_symbol(symbol)
                    if base is None:
                        raise ValueError(
                            f"Pinned symbol {symbol} must share quote asset "
                            f"{self.settings.quote_asset}"
                        )
                    await discovery.validate_symbol_assets(
                        symbol, base, self.settings.quote_asset
                    )
                    if symbol not in metadata:
                        metadata[symbol] = {
                            "symbol": symbol,
                            "base_asset": base,
                            "quote_asset": self.settings.quote_asset,
                            "quote_volume": 0.0,
                            "price_change_percent": 0.0,
                            "last_price": None,
                        }
                    metadata[symbol]["pinned"] = True
                    ordered.append(symbol)
            finally:
                await discovery.close()

            tracked = self.portfolio.db.tracked_symbols(mode=self.settings.mode)
            for symbol in tracked:
                if self.settings.base_for_symbol(symbol) is None:
                    raise RuntimeError(
                        f"Tracked position/order {symbol} does not match "
                        f"QUOTE_ASSET={self.settings.quote_asset}; reconcile it before "
                        "changing the market universe"
                    )
                if symbol not in metadata:
                    metadata[symbol] = {
                        "symbol": symbol,
                        "base_asset": self.settings.base_for_symbol(symbol),
                        "quote_asset": self.settings.quote_asset,
                        "quote_volume": 0.0,
                        "price_change_percent": 0.0,
                        "last_price": None,
                    }
                metadata[symbol]["tracked"] = True
                ordered.append(symbol)
            selected = list(dict.fromkeys(ordered))
            if not selected:
                raise RuntimeError(
                    "No eligible Spot markets were discovered. Lower "
                    "UNIVERSE_MIN_QUOTE_VOLUME, pin supported symbols, or use configured mode."
                )
        else:
            selected = self.settings.trading_symbols
            metadata = {
                symbol: {
                    "symbol": symbol,
                    "base_asset": self.settings.base_for_symbol(symbol),
                    "quote_asset": self.settings.quote_asset,
                    "quote_volume": None,
                    "price_change_percent": None,
                    "last_price": None,
                }
                for symbol in selected
            }

        if (
            self.settings.mode == "live"
            and len(selected) > 1
            and not self.settings.allow_multi_symbol_live
        ):
            raise RuntimeError(
                "Dynamic universe exceeds one market in live mode; set "
                "ALLOW_MULTI_SYMBOL_LIVE=true only after Testnet validation."
            )

        universe_changed = set(self.portfolio.symbols) != set(selected)
        old_bots = self.bots
        next_bots: dict[str, TradingOrchestrator] = {}
        for symbol in selected:
            if symbol in old_bots:
                next_bots[symbol] = old_bots[symbol]
            else:
                next_bots[symbol] = self._build_bot(symbol, len(selected))
        for symbol, bot in old_bots.items():
            if symbol not in next_bots:
                await bot.exchange.close()

        self.bots = next_bots
        self.portfolio.bots = self.bots
        self.portfolio.symbols = list(selected)
        if universe_changed:
            self.portfolio.invalidate_account()
        self.portfolio.db.save_trading_universe(
            mode=self.settings.mode,
            symbols=selected,
        )
        self.universe_metadata = metadata
        self.universe_error = None
        self.universe_refreshed_at = datetime.now(UTC).isoformat()
        self._universe_refreshed_monotonic = time.monotonic()
        logger.info(
            "Universe refreshed: %d symbol(s) selected [%s]",
            len(selected),
            ", ".join(selected),
        )
        return self.universe_snapshot()

    async def refresh_universe(self, *, force: bool = True) -> dict:
        async with self._control_lock:
            try:
                return await self._refresh_universe_unlocked(force=force)
            except Exception as exc:
                self.universe_error = str(exc)
                raise

    def universe_snapshot(self) -> dict:
        return {
            "mode": self.settings.universe_mode,
            "ready": self.settings.universe_mode == "configured"
            or self._universe_refreshed_monotonic is not None,
            "symbols": self.symbols,
            "markets": [
                self.universe_metadata.get(symbol, {"symbol": symbol})
                for symbol in self.symbols
            ],
            "max_symbols": self.settings.universe_max_symbols,
            "min_quote_volume": self.settings.universe_min_quote_volume,
            "refreshed_at": self.universe_refreshed_at,
            "error": self.universe_error,
        }

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

    async def refresh_stopped_market_prices(self) -> dict[str, str]:
        """Value every configured coin without starting strategies or placing orders."""
        async with self._dashboard_price_lock:
            if self.settings.universe_mode == "top_volume":
                stale_symbols = []
                for symbol, bot in self.bots.items():
                    if bot.running:
                        continue
                    snapshot = bot.market_snapshot()
                    age = snapshot.get("price_age_ms")
                    if (
                        snapshot.get("price") is None
                        or age is None
                        or age > bot.settings.market_data_stale_seconds * 1000
                    ):
                        stale_symbols.append(symbol)
                if not stale_symbols:
                    return {}
                try:
                    prices = await self.primary_bot.exchange.ticker_prices(stale_symbols)
                except Exception as exc:
                    return {symbol: str(exc) for symbol in stale_symbols}
                errors = {}
                for symbol in stale_symbols:
                    price = prices.get(symbol)
                    bot = self.bots[symbol]
                    if price is None:
                        errors[symbol] = "Batch ticker response did not include this market"
                    elif not bot.running:
                        bot.latest_price = price
                        bot.latest_price_event_ms = int(time.time() * 1000)
                        bot.latest_price_received_monotonic = time.monotonic()
                        bot.latest_price_source = "rest-dashboard"
                return errors

            async def refresh(symbol, bot):
                if bot.running:
                    return None
                snapshot = bot.market_snapshot()
                age = snapshot.get("price_age_ms")
                if snapshot.get("price") is not None and age is not None and age <= bot.settings.market_data_stale_seconds * 1000:
                    return None
                try:
                    price = await bot.exchange.ticker_price(symbol)
                    if not math.isfinite(price) or price <= 0:
                        raise ValueError("Invalid ticker price")
                    # Starting the engine while the request is in flight transfers
                    # price ownership back to its market loop.
                    if not bot.running:
                        bot.latest_price = price
                        bot.latest_price_event_ms = int(time.time() * 1000)
                        bot.latest_price_received_monotonic = time.monotonic()
                        bot.latest_price_source = "rest-dashboard"
                except Exception as exc:
                    return symbol, str(exc)
                return None
            results = await asyncio.gather(*(refresh(symbol, bot) for symbol, bot in self.bots.items()))
            return dict(result for result in results if result is not None)

    async def start(self) -> dict[str, bool]:
        async with self._control_lock:
            return await self._start_unlocked()

    async def _start_unlocked(self) -> dict[str, bool]:
        if self.running:
            return {symbol: False for symbol in self.symbols}
        started: dict[str, bool] = {}
        started_bots: list[TradingOrchestrator] = []
        self._runtime_lock.acquire()
        try:
            if self.settings.universe_mode == "top_volume":
                await self._refresh_universe_unlocked(force=True)
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
            self._runtime_lock.release()
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
        for symbol, result in zip(symbols, results, strict=True):
            output[symbol] = False if isinstance(result, Exception) else bool(result)
        await self.portfolio.close()
        self._runtime_lock.release()
        return output

    async def analyze_all(self) -> dict[str, dict]:
        symbols = list(self.bots)
        results = await asyncio.gather(
            *(self.bots[symbol].analyze_once() for symbol in symbols),
            return_exceptions=True,
        )
        output: dict[str, dict] = {}
        for symbol, result in zip(symbols, results, strict=True):
            if isinstance(result, Exception):
                output[symbol] = {"error": str(result)}
            else:
                output[symbol] = result
        return output

    def overview(self) -> list[dict]:
        rows = []
        rank = {symbol: index + 1 for index, symbol in enumerate(self.symbols)}
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
                    "quote_volume": self.universe_metadata.get(symbol, {}).get("quote_volume"),
                    "price_change_percent": self.universe_metadata.get(symbol, {}).get("price_change_percent"),
                    "universe_rank": rank.get(symbol),
                    "pinned": bool(self.universe_metadata.get(symbol, {}).get("pinned")),
                    "tracked": bool(self.universe_metadata.get(symbol, {}).get("tracked")),
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
