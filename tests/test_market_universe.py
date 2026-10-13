import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.multi_orchestrator import MultiSymbolTradingManager
from app.config import Settings
from app.exchange.binance import BinanceClient


def test_dynamic_live_universe_requires_multi_symbol_opt_in(tmp_path):
    with pytest.raises(ValueError, match="Multi-symbol live trading is blocked"):
        Settings(
            _env_file=None,
            mode="live",
            allow_live_trading=True,
            live_protection_validated=True,
            allow_multi_symbol_live=False,
            universe_mode="top_volume",
            universe_max_symbols=5,
            binance_api_key="test",
            binance_api_secret="test",
            database_path=str(tmp_path / "live.db"),
        )


def test_single_ranked_symbol_plus_a_pin_still_requires_live_multi_symbol_gate(tmp_path):
    with pytest.raises(ValueError, match="Multi-symbol live trading is blocked"):
        Settings(
            _env_file=None,
            mode="live",
            allow_live_trading=True,
            live_protection_validated=True,
            allow_multi_symbol_live=False,
            universe_mode="top_volume",
            universe_max_symbols=1,
            universe_pinned_symbols="ETHUSDT",
            binance_api_key="test",
            binance_api_secret="test",
            database_path=str(tmp_path / "live-pin.db"),
        )


def test_tracked_positions_cannot_bypass_live_multi_symbol_gate(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="live",
            allow_live_trading=True,
            live_protection_validated=True,
            allow_multi_symbol_live=False,
            universe_mode="top_volume",
            universe_max_symbols=1,
            database_path=str(tmp_path / "live-tracked.db"),
            learning_profile_path=str(tmp_path / "profile.json"),
            binance_api_key="test",
            binance_api_secret="test",
        )
        manager = MultiSymbolTradingManager(settings)
        manager.portfolio.db.open_trade(
            mode="live", symbol="ETHUSDT", quantity=0.01,
            entry_price=100, entry_fee=0, reason="tracked",
            stop_price=95, take_profit_price=110,
        )
        with patch.object(
            BinanceClient,
            "discover_top_volume_symbols",
            new=AsyncMock(return_value=[{
                "symbol": "BTCUSDT", "base_asset": "BTC", "quote_asset": "USDT",
                "quote_volume": 10_000_000, "price_change_percent": 1, "last_price": 70_000,
            }]),
        ):
            with pytest.raises(RuntimeError, match="exceeds one market in live mode"):
                await manager.refresh_universe()
        for bot in manager.bots.values():
            await bot.exchange.close()

    asyncio.run(scenario())


def test_dynamic_universe_parser_normalizes_pins_and_exclusions():
    settings = Settings(
        _env_file=None,
        universe_mode="top_volume",
        symbols="ethusdt, BTCUSDT,ETHUSDT",
        universe_pinned_symbols=" solusdt, BTCUSDT ",
        universe_excluded_bases="usdc, BUSD,usdc",
    )
    assert settings.configured_symbols == ["ETHUSDT", "BTCUSDT"]
    assert settings.pinned_trading_symbols == ["SOLUSDT", "BTCUSDT"]
    assert settings.excluded_base_assets == {"USDC", "BUSD"}


def test_discovery_ranks_quote_volume_and_filters_non_spot_wrappers():
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        client._request = AsyncMock(side_effect=[
            {"symbols": [
                {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True, "orderTypes": ["MARKET"]},
                {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True, "orderTypes": ["MARKET"]},
                {"symbol": "USDCUSDT", "baseAsset": "USDC", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True, "orderTypes": ["MARKET"]},
                {"symbol": "BTCUPUSDT", "baseAsset": "BTCUP", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True, "orderTypes": ["MARKET"]},
                {"symbol": "DOGEUSDT", "baseAsset": "DOGE", "quoteAsset": "USDT", "status": "BREAK", "isSpotTradingAllowed": True, "orderTypes": ["MARKET"]},
                {"symbol": "XRPUSDT", "baseAsset": "XRP", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": False, "orderTypes": ["MARKET"]},
                {"symbol": "BTCFDUSD", "baseAsset": "BTC", "quoteAsset": "FDUSD", "status": "TRADING", "isSpotTradingAllowed": True, "orderTypes": ["MARKET"]},
            ]},
            [
                {"symbol": "BTCUSDT", "quoteVolume": "9000000", "lastPrice": "70000", "priceChangePercent": "2.5"},
                {"symbol": "ETHUSDT", "quoteVolume": "12000000", "lastPrice": "3000", "priceChangePercent": "-1.0"},
                {"symbol": "USDCUSDT", "quoteVolume": "50000000", "lastPrice": "1", "priceChangePercent": "0.01"},
                {"symbol": "BTCUPUSDT", "quoteVolume": "30000000", "lastPrice": "2", "priceChangePercent": "3"},
                {"symbol": "DOGEUSDT", "quoteVolume": "100000000", "lastPrice": "0.2", "priceChangePercent": "5"},
                {"symbol": "XRPUSDT", "quoteVolume": "25000000", "lastPrice": "0.5", "priceChangePercent": "-2"},
            ],
        ])

        result = await client.discover_top_volume_symbols(
            "USDT", limit=5, min_quote_volume=1_000_000,
            excluded_bases={"USDC", "FDUSD"},
        )
        assert [item["symbol"] for item in result] == ["ETHUSDT", "BTCUSDT"]
        assert result[0]["quote_volume"] == 12_000_000
        assert result[0]["price_change_percent"] == -1.0
        client._request.assert_any_await("GET", "/api/v3/exchangeInfo")
        client._request.assert_any_await("GET", "/api/v3/ticker/24hr")
        await client.close()

    asyncio.run(scenario())


def test_testnet_discovery_uses_testnet_markets_ranked_by_live_spot_turnover():
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="testnet",
            binance_api_key="test",
            binance_api_secret="test",
        )
        client = BinanceClient(settings)
        calls = []

        async def fake_request(self, method, path, params=None, signed=False):
            calls.append((self.base_url, path))
            if self.base_url == "https://testnet.binance.vision":
                return {"symbols": [
                    {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True, "orderTypes": ["MARKET"]},
                    {"symbol": "ALTUSDT", "baseAsset": "ALT", "quoteAsset": "USDT", "status": "BREAK", "isSpotTradingAllowed": True, "orderTypes": ["MARKET"]},
                ]}
            return [
                {"symbol": "BTCUSDT", "quoteVolume": "50000000", "lastPrice": "70000", "priceChangePercent": "1"},
                {"symbol": "ALTUSDT", "quoteVolume": "100000000", "lastPrice": "2", "priceChangePercent": "4"},
            ]

        with patch.object(BinanceClient, "_request", new=fake_request):
            result = await client.discover_top_volume_symbols("USDT", limit=5, min_quote_volume=1)
        assert [item["symbol"] for item in result] == ["BTCUSDT"]
        assert ("https://testnet.binance.vision", "/api/v3/exchangeInfo") in calls
        assert ("https://api.binance.com", "/api/v3/ticker/24hr") in calls
        await client.close()

    asyncio.run(scenario())


def test_asset_quote_pairs_resolve_direct_and_inverse_markets():
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        client._request = AsyncMock(return_value={"symbols": [
            {"symbol": "BNBUSDT", "baseAsset": "BNB", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True},
            {"symbol": "USDTTRY", "baseAsset": "USDT", "quoteAsset": "TRY", "status": "TRADING", "isSpotTradingAllowed": True},
            {"symbol": "OLDUSDT", "baseAsset": "OLD", "quoteAsset": "USDT", "status": "BREAK", "isSpotTradingAllowed": True},
            {"symbol": "FAKEUSDT", "baseAsset": "FAKE", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": False},
        ]})
        pairs = await client.asset_quote_conversion_pairs(
            ["BNB", "TRY", "OLD", "FAKE", "USDT"], "USDT"
        )
        assert pairs == {
            "BNB": {"symbol": "BNBUSDT", "inverse": False},
            "TRY": {"symbol": "USDTTRY", "inverse": True},
        }
        await client.close()

    asyncio.run(scenario())


def test_batch_ticker_prices_validates_and_maps_symbols():
    async def scenario():
        client = BinanceClient(Settings(_env_file=None))
        client._request = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "price": "70000"},
            {"symbol": "ETHUSDT", "price": "3000"},
            {"symbol": "NOT-REQUESTED", "price": "1"},
            {"symbol": "SOLUSDT", "price": "NaN"},
        ])
        result = await client.ticker_prices(["BTCUSDT", "ETHUSDT", "BTCUSDT", "SOLUSDT"])
        assert result == {"BTCUSDT": 70000.0, "ETHUSDT": 3000.0}
        params = client._request.await_args.args[2]
        assert params["symbols"] == '["BTCUSDT","ETHUSDT","SOLUSDT"]'
        await client.close()

    asyncio.run(scenario())


def test_dynamic_refresh_keeps_tracked_positions_and_adds_ranked_pairs(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="paper",
            symbol="BTCUSDT",
            symbols="",
            universe_mode="top_volume",
            universe_max_symbols=2,
            universe_pinned_symbols="ADAUSDT",
            database_path=str(tmp_path / "dynamic.db"),
            learning_profile_path=str(tmp_path / "profile.json"),
            enable_adaptive_learning=False,
            enable_llm_advisor=False,
        )
        manager = MultiSymbolTradingManager(settings)
        btc = manager.get_bot("BTCUSDT")
        manager.portfolio.db.open_trade(
            mode="paper", symbol="BTCUSDT", quantity=0.01,
            entry_price=100, entry_fee=0, reason="tracked",
            stop_price=95, take_profit_price=110,
        )

        with patch.object(
            BinanceClient,
            "discover_top_volume_symbols",
            new=AsyncMock(return_value=[
                {"symbol": "ETHUSDT", "base_asset": "ETH", "quote_asset": "USDT",
                 "quote_volume": 20_000_000.0, "price_change_percent": 2.0, "last_price": 3000.0},
                {"symbol": "SOLUSDT", "base_asset": "SOL", "quote_asset": "USDT",
                 "quote_volume": 15_000_000.0, "price_change_percent": -1.0, "last_price": 150.0},
            ]),
        ), patch.object(BinanceClient, "validate_symbol_assets", new=AsyncMock()):
            result = await manager.refresh_universe()

        assert result["ready"] is True
        assert manager.symbols == ["ETHUSDT", "SOLUSDT", "ADAUSDT", "BTCUSDT"]
        assert manager.get_bot("BTCUSDT") is btc
        assert manager.portfolio.symbols == manager.symbols
        assert result["markets"][0]["quote_volume"] == 20_000_000.0
        assert result["markets"][2]["pinned"] is True
        assert manager.portfolio.db.saved_trading_universe(mode="paper") == manager.symbols
        assert manager.overview()[0]["universe_rank"] == 1
        for bot in manager.bots.values():
            await bot.exchange.close()

    asyncio.run(scenario())


def test_failed_dynamic_discovery_obeys_retry_interval_and_surfaces_error(tmp_path):
    async def scenario():
        settings = Settings(
            _env_file=None,
            mode="paper",
            universe_mode="top_volume",
            universe_refresh_minutes=60,
            database_path=str(tmp_path / "failed-discovery.db"),
            learning_profile_path=str(tmp_path / "profile.json"),
        )
        manager = MultiSymbolTradingManager(settings)
        discovery = AsyncMock(side_effect=RuntimeError("public market data offline"))
        with patch.object(BinanceClient, "discover_top_volume_symbols", new=discovery):
            with pytest.raises(RuntimeError, match="public market data offline"):
                await manager.refresh_universe(force=False)
            snapshot = await manager.refresh_universe(force=False)
        assert discovery.await_count == 1
        assert snapshot["error"] == "public market data offline"
        assert snapshot["ready"] is False
        for bot in manager.bots.values():
            await bot.exchange.close()

    asyncio.run(scenario())


def test_configured_universe_refresh_is_offline_and_stable(tmp_path):
    settings = Settings(
        _env_file=None,
        mode="paper",
        symbols="BTCUSDT,ETHUSDT",
        universe_mode="configured",
        database_path=str(tmp_path / "configured.db"),
        learning_profile_path=str(tmp_path / "profile.json"),
    )
    manager = MultiSymbolTradingManager(settings)
    result = asyncio.run(manager.refresh_universe())
    assert manager.symbols == ["BTCUSDT", "ETHUSDT"]
    assert result["ready"] is True
    assert result["mode"] == "configured"
