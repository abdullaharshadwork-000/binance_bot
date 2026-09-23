import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import urlencode

import httpx
import pandas as pd
import websockets

from app.config import Settings


class BinanceClient:
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
        return float(data["price"])

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

    async def exchange_info(self, symbol: str) -> dict:
        if self._exchange_info_cache is None:
            self._exchange_info_cache = await self._request("GET", "/api/v3/exchangeInfo")
        for item in self._exchange_info_cache.get("symbols", []):
            if item.get("symbol") == symbol:
                return item
        raise ValueError(f"Unknown Binance symbol: {symbol}")

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
        filters = await self.symbol_filters(symbol)

        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE") or {}
        step = Decimal(str(lot.get("stepSize", "0") or "0"))
        min_qty = Decimal(str(lot.get("minQty", "0") or "0"))
        max_qty = Decimal(str(lot.get("maxQty", "0") or "0"))

        if step <= 0:
            lot = filters.get("LOT_SIZE", {})
            step = Decimal(str(lot.get("stepSize", "0.00000001") or "0.00000001"))
            min_qty = Decimal(str(lot.get("minQty", "0") or "0"))
            max_qty = Decimal(str(lot.get("maxQty", "0") or "0"))

        normalized = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
        if normalized < min_qty:
            return Decimal("0")
        if max_qty > 0 and normalized > max_qty:
            return Decimal("0")

        notional = normalized * price
        notional_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        min_notional = Decimal(str(notional_filter.get("minNotional", "0") or "0"))
        max_notional = Decimal(str(notional_filter.get("maxNotional", "0") or "0"))
        if min_notional > 0 and notional < min_notional:
            return Decimal("0")
        if max_notional > 0 and notional > max_notional:
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

    async def asset_balance(self, asset: str) -> float:
        """Return free balance. Use asset_balances() for total free + locked equity."""
        account = await self.account()
        for balance in account.get("balances", []):
            if balance["asset"] == asset:
                return float(balance.get("free") or 0.0)
        return 0.0

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

    @staticmethod
    def _format_qty(quantity: float) -> str:
        return format(Decimal(str(quantity)).normalize(), "f")

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
        return [
            {
                "asset": str(fill.get("commissionAsset") or ""),
                "amount": float(Decimal(str(fill.get("commission") or "0"))),
            }
            for fill in (order.get("fills") or [])
            if Decimal(str(fill.get("commission") or "0")) > 0
        ]

    async def order_fee_quote(self, order: dict, fill_price: float) -> float:
        """Convert fill commissions to quote currency, including third-asset fees when possible."""
        fills = order.get("fills") or []
        if not fills:
            return self.estimated_order_fee_quote(order, fill_price)

        total = Decimal("0")
        saw_commission_field = False
        for fill in fills:
            if "commission" not in fill:
                continue
            saw_commission_field = True
            commission = Decimal(str(fill.get("commission") or "0"))
            if commission <= 0:
                continue

            asset = str(fill.get("commissionAsset") or "")
            if asset == self.settings.quote_asset:
                total += commission
                continue
            if asset == self.settings.base_asset:
                total += commission * Decimal(str(fill.get("price") or fill_price))
                continue

            # Third-asset commissions (for example BNB) are valued using the
            # closest direct quote pair available. If neither pair exists, leave
            # the fee unconverted but preserve it in commission_details().
            try:
                conversion = Decimal(
                    str(await self.ticker_price(f"{asset}{self.settings.quote_asset}"))
                )
                total += commission * conversion
                continue
            except Exception:
                pass
            try:
                inverse = Decimal(
                    str(await self.ticker_price(f"{self.settings.quote_asset}{asset}"))
                )
                if inverse > 0:
                    total += commission / inverse
            except Exception:
                pass

        if saw_commission_field:
            return float(total)
        return self.estimated_order_fee_quote(order, fill_price)

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
