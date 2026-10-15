import asyncio
import hashlib
import hmac
import json
import logging
import math
import time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx
import pandas as pd
import websockets

from app.config import Settings


class BinanceClient:
    _blocked_until: dict[str, float] = {}
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = (
            "https://testnet.binance.vision"
            if settings.mode == "testnet"
            else "https://api.binance.com"
        )
        self.ws_base_url = (
            "wss://stream.testnet.binance.vision/ws"
            if settings.mode == "testnet"
            else "wss://stream.binance.com:9443/ws"
        )
        self._exchange_info_cached_at = 0.0
        self._exchange_info_cache: dict | None = None
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        if time.monotonic() < self._blocked_until.get(self.base_url, 0):
            raise RuntimeError("Binance rate-limit cooldown is active")
        params = dict(params or {})
        headers = {}
        if signed:
            if not (self.settings.binance_api_key and self.settings.binance_api_secret):
                raise RuntimeError("Binance API credentials are missing")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            query = urlencode(params, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            params["signature"] = signature
            headers["X-MBX-APIKEY"] = self.settings.binance_api_key

        response = await self._http().request(method, path, params=params, headers=headers)
        if response.status_code in {418, 429}:
            try:
                delay = float(response.headers.get("Retry-After", "60"))
            except ValueError:
                delay = 60.0
            if not math.isfinite(delay) or delay <= 0:
                delay = 60.0
            self._blocked_until[self.base_url] = time.monotonic() + delay
            logging.getLogger(__name__).warning(
                "Binance rate limit %s on %s %s; all requests to this endpoint "
                "pause for %.0fs (Retry-After)",
                response.status_code,
                method,
                path,
                delay,
            )
        if response.is_error:
            raise RuntimeError(f"Binance {response.status_code}: {response.text}")
        return response.json()

    async def ping(self) -> bool:
        await self._request("GET", "/api/v3/ping")
        return True

    async def klines(self, symbol: str, interval: str, limit: int = 250) -> pd.DataFrame:
        data = await self._request(
            "GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )
        rows = []
        for k in data:
            rows.append(
                {
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": int(k[6]),
                }
            )
        return pd.DataFrame(rows)

    async def ticker_price(self, symbol: str) -> float:
        data = await self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        price = float(data["price"])
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid Binance ticker price")
        return price

    async def stream_trade_prices(self, symbol: str, on_tick) -> None:
        """Continuously stream real-time aggregate trade prices and reconnect on transient failures."""
        stream = f"{symbol.lower()}@aggTrade"
        url = f"{self.ws_base_url}/{stream}"
        retry_delay = 1.0

        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=256,
                ) as ws:
                    retry_delay = 1.0
                    async for raw in ws:
                        payload = json.loads(raw)
                        price = float(payload["p"])
                        event_time = int(payload.get("E") or payload.get("T") or int(time.time() * 1000))
                        await on_tick(price, event_time)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 15.0)

    async def _exchange_info(self) -> dict:
        if self._exchange_info_cache is None or time.monotonic() - self._exchange_info_cached_at >= 60:
            self._exchange_info_cache = await self._request("GET", "/api/v3/exchangeInfo")
            self._exchange_info_cached_at = time.monotonic()
        return self._exchange_info_cache

    async def exchange_info(self, symbol: str) -> dict:
        info = await self._exchange_info()
        for item in info.get("symbols", []):
            if item.get("symbol") == symbol:
                return item
        raise ValueError(f"Unknown Binance symbol: {symbol}")

    async def asset_quote_conversion_pairs(
        self,
        assets: list[str],
        quote_asset: str,
    ) -> dict[str, dict]:
        """Find active direct/inverse Spot pairs for valuing other account assets."""
        quote = quote_asset.strip().upper()
        wanted = {str(asset).strip().upper() for asset in assets if str(asset).strip()}
        wanted.discard(quote)
        info = await self._exchange_info()
        markets: dict[tuple[str, str], dict] = {}
        for market in info.get("symbols", []):
            if (
                market.get("status") != "TRADING"
                or market.get("isSpotTradingAllowed") is False
            ):
                continue
            base = str(market.get("baseAsset") or "").upper()
            market_quote = str(market.get("quoteAsset") or "").upper()
            symbol = str(market.get("symbol") or "").upper()
            if base and market_quote and symbol:
                markets[(base, market_quote)] = market

        result: dict[str, dict] = {}
        for asset in sorted(wanted):
            direct = markets.get((asset, quote))
            inverse = markets.get((quote, asset))
            market = direct or inverse
            if market is None:
                continue
            result[asset] = {
                "symbol": str(market["symbol"]).upper(),
                "inverse": direct is None,
            }
        return result

    async def discover_top_volume_symbols(
        self,
        quote_asset: str,
        *,
        limit: int = 15,
        min_quote_volume: float = 1_000_000.0,
        excluded_bases: set[str] | None = None,
    ) -> list[dict]:
        """Return the most liquid eligible Spot pairs for one quote asset.

        Ranking uses Binance's rolling 24-hour quote volume. Leveraged tokens and
        configured stable-asset bases are excluded to avoid wasting the universe
        on synthetic/near-flat markets. This is market discovery, not a buy signal.
        """
        quote = quote_asset.strip().upper()
        if not quote or limit < 1 or not math.isfinite(min_quote_volume) or min_quote_volume < 0:
            raise ValueError("Invalid quote asset or universe limits")

        # Spot Testnet has a reduced symbol set and often lacks meaningful
        # turnover. Keep its exchangeInfo as the tradability allowlist, but rank
        # those symbols using the corresponding public Spot rolling volume.
        info = await self._request("GET", "/api/v3/exchangeInfo")
        self._exchange_info_cache = info
        self._exchange_info_cached_at = time.monotonic()
        if self.settings.mode == "testnet":
            public_client = BinanceClient(self.settings.model_copy(update={"mode": "paper"}))
            try:
                tickers = await public_client._request("GET", "/api/v3/ticker/24hr")
            finally:
                await public_client.close()
        else:
            tickers = await self._request("GET", "/api/v3/ticker/24hr")
        ticker_by_symbol = {
            str(item.get("symbol") or ""): item
            for item in (tickers if isinstance(tickers, list) else [])
        }
        excluded = {str(asset).strip().upper() for asset in (excluded_bases or set())}
        excluded.update({quote})
        candidates: list[dict] = []
        leveraged_suffixes = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")

        for market in info.get("symbols", []):
            symbol = str(market.get("symbol") or "").upper()
            base = str(market.get("baseAsset") or "").upper()
            if (
                not symbol
                or not base
                or market.get("quoteAsset") != quote
                or market.get("status") != "TRADING"
                or market.get("isSpotTradingAllowed") is False
                or "MARKET" not in market.get("orderTypes", [])
                or base in excluded
                or base.endswith(leveraged_suffixes)
            ):
                continue

            ticker = ticker_by_symbol.get(symbol)
            if ticker is None:
                continue
            try:
                quote_volume = float(Decimal(str(ticker.get("quoteVolume") or "0")))
                last_price = float(Decimal(str(ticker.get("lastPrice") or "0")))
                change = float(Decimal(str(ticker.get("priceChangePercent") or "0")))
            except Exception:
                continue
            if (
                not math.isfinite(quote_volume)
                or not math.isfinite(last_price)
                or not math.isfinite(change)
                or quote_volume < min_quote_volume
                or quote_volume <= 0
                or last_price <= 0
            ):
                continue
            candidates.append({
                "symbol": symbol,
                "base_asset": base,
                "quote_asset": quote,
                "quote_volume": quote_volume,
                "price_change_percent": change,
                "last_price": last_price,
            })

        candidates.sort(key=lambda item: (-item["quote_volume"], item["symbol"]))
        return candidates[:limit]

    async def ticker_prices(self, symbols: list[str]) -> dict[str, float]:
        """Fetch a batch of current prices so a large watchlist avoids N REST calls."""
        normalized = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()))
        if not normalized:
            return {}
        payload = await self._request(
            "GET",
            "/api/v3/ticker/price",
            {"symbols": json.dumps(normalized, separators=(",", ":"))},
        )
        rows = payload if isinstance(payload, list) else [payload]
        result: dict[str, float] = {}
        for row in rows:
            try:
                symbol = str(row.get("symbol") or "").upper()
                price = float(Decimal(str(row.get("price") or "0")))
            except Exception:
                continue
            if symbol in normalized and math.isfinite(price) and price > 0:
                result[symbol] = price
        return result

    async def validate_symbol_assets(self, symbol: str, base_asset: str, quote_asset: str) -> None:
        info = await self.exchange_info(symbol)
        actual_base = str(info.get("baseAsset") or "")
        actual_quote = str(info.get("quoteAsset") or "")
        if actual_base != base_asset or actual_quote != quote_asset:
            raise ValueError(
                f"Configured assets do not match {symbol}: Binance reports "
                f"{actual_base}/{actual_quote}, configured {base_asset}/{quote_asset}"
            )

    async def symbol_filters(self, symbol: str) -> dict[str, dict]:
        info = await self.exchange_info(symbol)
        return {f["filterType"]: f for f in info.get("filters", [])}

    async def normalize_quantity_decimal(self, symbol: str, quantity: Decimal, price: Decimal) -> Decimal:
        if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
            return Decimal("0")
        filters = await self.symbol_filters(symbol)
        lots = [filters[key] for key in ("LOT_SIZE", "MARKET_LOT_SIZE") if key in filters]
        if not lots:
            raise ValueError("Missing Binance quantity filters")
        steps = [Decimal(str(lot.get("stepSize", "0"))) for lot in lots]
        steps = [step for step in steps if step > 0]
        normalized = quantity
        if steps:
            # The market filter can disable its step while keeping a max quantity.
            # Respect both filters, including non-divisible step combinations.
            scale = Decimal(10) ** max(0, max(-step.as_tuple().exponent for step in steps))
            step = Decimal(math.lcm(*(int(step * scale) for step in steps))) / scale
            normalized = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
        for lot in lots:
            minimum = Decimal(str(lot.get("minQty", "0")))
            maximum = Decimal(str(lot.get("maxQty", "0")))
            if normalized < minimum or (maximum > 0 and normalized > maximum):
                return Decimal("0")
        notional = normalized * price
        for name in ("MIN_NOTIONAL", "NOTIONAL"):
            rule = filters.get(name)
            if not rule:
                continue
            apply_min = rule.get("applyToMarket" if name == "MIN_NOTIONAL" else "applyMinToMarket", True)
            minimum = Decimal(str(rule.get("minNotional", "0")))
            maximum = Decimal(str(rule.get("maxNotional", "0")))
            if apply_min and minimum > 0 and notional < minimum:
                return Decimal("0")
            if rule.get("applyMaxToMarket", True) and maximum > 0 and notional > maximum:
                return Decimal("0")
        return normalized

    async def normalize_quantity(self, symbol: str, quantity: float, price: float) -> float:
        normalized = await self.normalize_quantity_decimal(
            symbol, Decimal(str(quantity)), Decimal(str(price))
        )
        return float(normalized)

    async def normalize_price(self, symbol: str, price: float) -> float:
        filters = await self.symbol_filters(symbol)
        pf = filters.get("PRICE_FILTER", {})
        tick = Decimal(str(pf.get("tickSize", "0.00000001")))
        if tick <= 0:
            return float(price)
        value = Decimal(str(price))
        normalized = (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
        return float(normalized)

    async def account(self) -> dict:
        return await self._request("GET", "/api/v3/account", signed=True)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        # Query account-wide open orders and filter locally. Spot Testnet can
        # reject the optional symbol parameter even when live REST supports it.
        data = await self._request(
            "GET",
            "/api/v3/openOrders",
            signed=True,
        )
        orders = list(data or [])
        if symbol is None:
            return orders
        return [order for order in orders if order.get("symbol") == symbol]

    async def check_entry_liquidity(self, symbol: str, quantity: float, reference_price: float) -> dict:
        """Check visible depth before buying; this cannot guarantee the eventual market fill."""
        if not all(math.isfinite(x) and x > 0 for x in (quantity, reference_price)):
            raise ValueError("Invalid entry size or reference price")
        started = time.monotonic()
        book = await self._request("GET", "/api/v3/depth", {"symbol": symbol, "limit": 20})
        if time.monotonic() - started > self.settings.market_data_stale_seconds:
            raise ValueError("Order book request was too slow; resize using fresh prices")
        try:
            bids = [(Decimal(str(p)), Decimal(str(q))) for p, q in book["bids"]]
            asks = [(Decimal(str(p)), Decimal(str(q))) for p, q in book["asks"]]
            if not bids or not asks or any(not x.is_finite() or x <= 0 for level in bids + asks for x in level):
                raise ValueError("Invalid or empty order book")
            if any(asks[i][0] > asks[i + 1][0] for i in range(len(asks) - 1)):
                raise ValueError("Unsorted order book")
            if any(bids[i][0] < bids[i + 1][0] for i in range(len(bids) - 1)):
                raise ValueError("Unsorted order book")
        except (KeyError, TypeError, ArithmeticError) as exc:
            raise ValueError("Invalid order book") from exc
        bid, ask = bids[0][0], asks[0][0]
        if bid >= ask:
            raise ValueError("Crossed order book")
        spread_bps = float((ask - bid) / ((ask + bid) / 2) * 10000)
        if spread_bps > self.settings.max_entry_spread_bps:
            raise ValueError(f"Entry spread {spread_bps:.2f} bps exceeds configured limit")
        remaining = Decimal(str(quantity))
        cost = Decimal("0")
        for price, available in asks:
            used = min(remaining, available)
            cost += used * price
            remaining -= used
            if remaining == 0:
                break
        if remaining > 0:
            raise ValueError("Insufficient visible order-book depth for entry size")
        estimate = float(cost / Decimal(str(quantity)))
        slippage_bps = (estimate / reference_price - 1) * 10000
        if abs(slippage_bps) > self.settings.max_entry_slippage_bps:
            raise ValueError(f"Entry price moved {slippage_bps:.2f} bps; resize using fresh prices")
        return {"spread_bps": spread_bps, "estimated_fill_price": estimate,
                "estimated_slippage_bps": slippage_bps}

    async def asset_balance(self, asset: str) -> float:
        """Return free balance. Use asset_balances() for total free + locked equity."""
        account = await self.account()
        for balance in account.get("balances", []):
            if balance["asset"] == asset:
                return float(balance.get("free") or 0.0)
        return 0.0

    async def asset_balance_details(self, asset: str) -> dict[str, float]:
        """Return free, locked and total balance for position reconciliation."""
        account = await self.account()
        for balance in account.get("balances", []):
            if balance.get("asset") == asset:
                free = Decimal(str(balance.get("free") or "0"))
                locked = Decimal(str(balance.get("locked") or "0"))
                return {
                    "free": float(free),
                    "locked": float(locked),
                    "total": float(free + locked),
                }
        return {"free": 0.0, "locked": 0.0, "total": 0.0}

    async def asset_balances(self, assets: set[str]) -> dict[str, float]:
        """Return total balances (free + locked) so equity includes exchange-held funds."""
        account = await self.account()
        result = {asset: 0.0 for asset in assets}
        for balance in account.get("balances", []):
            asset = balance.get("asset")
            if asset in result:
                free = Decimal(str(balance.get("free") or "0"))
                locked = Decimal(str(balance.get("locked") or "0"))
                result[asset] = float(free + locked)
        return result

    async def market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        *,
        client_order_id: str | None = None,
    ) -> dict:
        if self.settings.mode == "paper" or (self.settings.mode == "live" and not self.settings.allow_live_trading):
            raise RuntimeError("Exchange orders are disabled for this configuration")
        if symbol != self.settings.symbol or side not in {"BUY", "SELL"}:
            raise ValueError("Order symbol or side does not match configuration")
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("Order quantity must be positive and finite")
        info = await self.exchange_info(symbol)
        if info.get("status") != "TRADING" or not info.get("isSpotTradingAllowed") or "MARKET" not in info.get("orderTypes", []):
            raise RuntimeError("Symbol is not available for Spot market orders")
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": self._format_qty(quantity),
            "newOrderRespType": "FULL",
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        return await self._request("POST", "/api/v3/order", params, signed=True)

    async def get_order(self, symbol: str, *, client_order_id: str) -> dict:
        return await self._request(
            "GET",
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
            signed=True,
        )

    async def place_protective_oco(
        self,
        symbol: str,
        *,
        quantity: float,
        take_profit_price: float,
        stop_price: float,
        list_client_order_id: str,
        take_profit_client_order_id: str,
        stop_client_order_id: str,
    ) -> dict:
        if self.settings.mode not in {"testnet", "live"}:
            raise RuntimeError(
                "Exchange-resident protection requires Binance testnet or live mode"
            )
        if self.settings.mode == "live" and not self.settings.allow_live_trading:
            raise RuntimeError("Live exchange protection is disabled by configuration")
        if symbol != self.settings.symbol:
            raise ValueError("Protective order symbol does not match configuration")
        if not all(
            math.isfinite(x) and x > 0
            for x in (quantity, take_profit_price, stop_price)
        ):
            raise ValueError("Protective order values must be positive and finite")
        if stop_price >= take_profit_price:
            raise ValueError("Protective stop must be below take-profit price")

        return await self._request(
            "POST",
            "/api/v3/orderList/oco",
            {
                "symbol": symbol,
                "side": "SELL",
                "quantity": self._format_qty(quantity),
                "listClientOrderId": list_client_order_id,
                "aboveType": "LIMIT_MAKER",
                "aboveClientOrderId": take_profit_client_order_id,
                "abovePrice": self._format_price(take_profit_price),
                "belowType": "STOP_LOSS",
                "belowClientOrderId": stop_client_order_id,
                "belowStopPrice": self._format_price(stop_price),
                "newOrderRespType": "FULL",
            },
            signed=True,
        )

    async def get_order_list(
        self,
        symbol: str,
        *,
        list_client_order_id: str,
    ) -> dict:
        # Binance Spot Query Order List does not accept a symbol parameter.
        # Keep symbol in this method signature for call-site symmetry, but use
        # only the order-list client id in the signed request.
        return await self._request(
            "GET",
            "/api/v3/orderList",
            {
                "origClientOrderId": list_client_order_id,
            },
            signed=True,
        )

    async def cancel_order_list(
        self,
        symbol: str,
        *,
        list_client_order_id: str,
    ) -> dict:
        return await self._request(
            "DELETE",
            "/api/v3/orderList",
            {
                "symbol": symbol,
                "listClientOrderId": list_client_order_id,
            },
            signed=True,
        )

    async def my_trades(self, symbol: str, *, order_id: int | str) -> list[dict]:
        """Fetch account fills for one order so restart recovery can verify commissions."""
        data = await self._request(
            "GET",
            "/api/v3/myTrades",
            {"symbol": symbol, "orderId": order_id, "limit": 1000},
            signed=True,
        )
        return list(data or [])

    @staticmethod
    def _format_qty(quantity: float) -> str:
        return format(Decimal(str(quantity)).normalize(), "f")

    @staticmethod
    def _format_price(price: float) -> str:
        return format(Decimal(str(price)).normalize(), "f")

    @staticmethod
    def executed_quantity(order: dict) -> float:
        return float(Decimal(str(order.get("executedQty") or "0")))

    @staticmethod
    def weighted_fill_price(order: dict, fallback: float) -> float:
        fills = order.get("fills") or []
        qty = sum(Decimal(str(f.get("qty", "0"))) for f in fills)
        if qty > 0:
            value = sum(
                Decimal(str(f.get("price", "0"))) * Decimal(str(f.get("qty", "0")))
                for f in fills
            )
            return float(value / qty)
        executed = Decimal(str(order.get("executedQty") or "0"))
        quote = Decimal(str(order.get("cummulativeQuoteQty") or "0"))
        return float(quote / executed) if executed > 0 and quote > 0 else fallback

    def commission_details(self, order: dict) -> list[dict]:
        details = [
            {
                "asset": str(fill.get("commissionAsset") or ""),
                "amount": float(Decimal(str(fill.get("commission") or "0"))),
            }
            for fill in (order.get("fills") or [])
            if isinstance(fill, dict)
            and Decimal(str(fill.get("commission") or "0")) > 0
        ]
        valuation = order.get("_commission_quote_valuation")
        if isinstance(valuation, dict):
            details.append({"type": "QUOTE_FEE_VALUATION", **valuation})
        return details

    def _configured_fee_estimate_quote(
        self,
        order: dict,
        fills: list[dict],
        fill_price: float,
    ) -> Decimal:
        """Estimate total fees from the configured rate and order quote notional."""
        try:
            quote_notional = Decimal(str(order.get("cummulativeQuoteQty") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            quote_notional = Decimal("0")
        if not quote_notional.is_finite() or quote_notional <= 0:
            quote_notional = Decimal("0")
            for fill in fills:
                if not isinstance(fill, dict):
                    continue
                try:
                    price = Decimal(str(fill.get("price") or "0"))
                    quantity = Decimal(str(fill.get("qty") or "0"))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if price.is_finite() and quantity.is_finite() and price > 0 and quantity > 0:
                    quote_notional += price * quantity
        if quote_notional <= 0 and math.isfinite(fill_price) and fill_price > 0:
            try:
                quantity = Decimal(str(order.get("executedQty") or "0"))
            except (InvalidOperation, TypeError, ValueError):
                quantity = Decimal("0")
            if quantity.is_finite() and quantity > 0:
                quote_notional = quantity * Decimal(str(fill_price))
        rate = Decimal(str(self.settings.trading_fee_bps)) / Decimal("10000")
        return max(Decimal("0"), quote_notional * rate)

    async def order_fee_quote(self, order: dict, fill_price: float) -> float:
        """Value reported commissions; use a disclosed fee-rate estimate if needed."""
        fills = order.get("fills") or []
        if not fills:
            fee_quote = self.estimated_order_fee_quote(order, fill_price)
            order["_commission_quote_valuation"] = {
                "method": "configured_fee_rate_fallback",
                "estimated": True,
                "quote_asset": self.settings.quote_asset,
                "quote_amount": fee_quote,
                "unpriced_assets": [],
                "warning": "Exchange fill-level commissions were unavailable",
            }
            return fee_quote

        total = Decimal("0")
        third_asset_commissions: dict[str, Decimal] = {}
        saw_commission_field = False
        for fill in fills:
            if not isinstance(fill, dict) or "commission" not in fill:
                continue
            saw_commission_field = True
            try:
                commission = Decimal(str(fill.get("commission") or "0"))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("Invalid Binance fill commission") from exc
            if not commission.is_finite() or commission < 0:
                raise ValueError("Binance fill commission must be finite and non-negative")
            if commission == 0:
                continue

            asset = str(fill.get("commissionAsset") or "").strip().upper()
            if not asset:
                raise ValueError("Positive Binance commission is missing its asset")
            if asset == self.settings.quote_asset:
                total += commission
                continue
            if asset == self.settings.base_asset:
                try:
                    conversion = Decimal(str(fill.get("price") or fill_price))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise ValueError("Invalid fill price for base-asset commission") from exc
                if not conversion.is_finite() or conversion <= 0:
                    raise ValueError("Invalid fill price for base-asset commission")
                total += commission * conversion
                continue
            third_asset_commissions[asset] = (
                third_asset_commissions.get(asset, Decimal("0")) + commission
            )

        unpriced_assets: list[str] = []
        conversion_warnings: list[str] = []
        if third_asset_commissions:
            assets = sorted(third_asset_commissions)
            try:
                pairs = await self.asset_quote_conversion_pairs(
                    assets,
                    self.settings.quote_asset,
                )
            except Exception as exc:
                pairs = {}
                conversion_warnings.append(str(exc))

            available_pairs = {}
            for asset in assets:
                pair = pairs.get(asset)
                if isinstance(pair, dict) and pair.get("symbol"):
                    available_pairs[asset] = pair
                else:
                    unpriced_assets.append(asset)

            prices: dict[str, float] = {}
            if available_pairs:
                symbols = list(dict.fromkeys(
                    pair["symbol"] for pair in available_pairs.values()
                ))
                try:
                    prices = await self.ticker_prices(symbols)
                except Exception as exc:
                    conversion_warnings.append(str(exc))

            for asset, pair in available_pairs.items():
                try:
                    conversion = Decimal(str(prices.get(pair["symbol"])))
                except (InvalidOperation, TypeError, ValueError):
                    unpriced_assets.append(asset)
                    continue
                if not conversion.is_finite() or conversion <= 0:
                    unpriced_assets.append(asset)
                    continue
                commission = third_asset_commissions[asset]
                total += (
                    commission / conversion
                    if pair.get("inverse")
                    else commission * conversion
                )

        if saw_commission_field:
            if unpriced_assets:
                estimate = self._configured_fee_estimate_quote(
                    order,
                    fills,
                    fill_price,
                )
                total = max(total, estimate)
                warning = (
                    "Third-asset commission(s) could not be converted; total quote "
                    "fee uses the configured fee-rate estimate"
                )
                if conversion_warnings:
                    warning += ": " + "; ".join(conversion_warnings)
                order["_commission_quote_valuation"] = {
                    "method": "configured_fee_rate_fallback",
                    "estimated": True,
                    "quote_asset": self.settings.quote_asset,
                    "quote_amount": float(total),
                    "unpriced_assets": sorted(set(unpriced_assets)),
                    "warning": warning,
                }
                logging.getLogger(__name__).warning(warning)
            else:
                order["_commission_quote_valuation"] = {
                    "method": (
                        "fresh_direct_inverse_spot_quote"
                        if third_asset_commissions
                        else "reported_fill_commissions"
                    ),
                    "estimated": False,
                    "quote_asset": self.settings.quote_asset,
                    "quote_amount": float(total),
                    "unpriced_assets": [],
                }
            fee_quote = float(total)
            if not math.isfinite(fee_quote) or fee_quote < 0:
                raise ValueError("Converted Binance commission is invalid")
            return fee_quote

        fee_quote = self.estimated_order_fee_quote(order, fill_price)
        order["_commission_quote_valuation"] = {
            "method": "configured_fee_rate_fallback",
            "estimated": True,
            "quote_asset": self.settings.quote_asset,
            "quote_amount": fee_quote,
            "unpriced_assets": [],
            "warning": "Exchange fill-level commissions were incomplete",
        }
        return fee_quote

    def estimated_order_fee_quote(self, order: dict, fill_price: float) -> float:
        """Best-effort fee in quote currency. Third-asset fees remain recorded separately."""
        fills = order.get("fills") or []
        total = Decimal("0")
        has_any_fill_fee = False
        for fill in fills:
            commission = Decimal(str(fill.get("commission") or "0"))
            asset = str(fill.get("commissionAsset") or "")
            if commission < 0:
                continue
            if "commission" in fill:
                has_any_fill_fee = True
            if commission == 0:
                continue
            if asset == self.settings.quote_asset:
                total += commission
            elif asset == self.settings.base_asset:
                total += commission * Decimal(str(fill.get("price") or fill_price))

        if has_any_fill_fee:
            return float(total)

        quote_qty = Decimal(str(order.get("cummulativeQuoteQty") or "0"))
        return float(quote_qty * Decimal(str(self.settings.trading_fee_bps)) / Decimal("10000"))
