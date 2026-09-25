import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from app.agents.multi_orchestrator import MultiSymbolTradingManager
from app.config import Settings
from app.models import ExecutionResult, RiskDecision, SignalSide, StrategySignal
from app.risk.manager import RiskManager


def manager(tmp_path, **overrides):
    values = dict(_env_file=None, mode="paper", symbols="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT",
                  database_path=str(tmp_path / "db"), learning_profile_path=str(tmp_path / "profile.json"),
                  enable_adaptive_learning=False, enable_llm_advisor=False)
    values.update(overrides)
    return MultiSymbolTradingManager(Settings(**values))


def position(bot, quantity=1, entry=100):
    return bot.db.open_trade(mode=bot.settings.mode, symbol=bot.settings.symbol,
                             quantity=quantity, entry_price=entry, entry_fee=0,
                             reason="test", stop_price=entry * .95, take_profit_price=entry * 1.1,
                             state_updates={bot.broker._paper_base_key: quantity})


async def tick(bot, price):
    await bot._on_price_tick(price, int(time.time() * 1000))


def test_paper_portfolio_values_each_coin_once(tmp_path):
    async def scenario():
        m = manager(tmp_path)
        btc, eth = m.get_bot("BTCUSDT"), m.get_bot("ETHUSDT")
        position(btc, 2)
        position(eth, 3)
        await tick(btc, 100)
        await tick(eth, 10)
        # Legacy mixed-unit state must never influence valuation.
        btc.db.set_state("paper_base_balance", 99999)
        assert float(btc.broker._paper_base_dec()) == 2
        assert float(eth.broker._paper_base_dec()) == 3
        assert await m.portfolio.poll_equity() == 1230
        assert m.portfolio.snapshot()["exposure"] == 230
        eth.latest_price_event_ms -= 60000
        assert await m.portfolio.poll_equity() is None
    asyncio.run(scenario())


def test_shared_account_request_counts_cash_once_and_revalues_holdings(tmp_path):
    async def scenario():
        m = manager(tmp_path, mode="testnet", binance_api_key="test", binance_api_secret="test")
        await tick(m.get_bot("BTCUSDT"), 100)
        await tick(m.get_bot("ETHUSDT"), 10)
        exchange = m.primary_bot.exchange
        exchange.account = AsyncMock(return_value={"balances": [
            {"asset": "USDT", "free": "500", "locked": "100"},
            {"asset": "BTC", "free": "1", "locked": "1"},
            {"asset": "ETH", "free": "2", "locked": "0"}]})
        assert await m.portfolio.poll_equity() is None
        await m.portfolio._balance_task
        assert await m.portfolio.poll_equity() == 820
        assert await m.get_bot("ETHUSDT")._poll_equity(10) == 820
        assert exchange.account.await_count == 1
        await tick(m.get_bot("ETHUSDT"), 20)
        assert await m.portfolio.poll_equity() == 840
        m.portfolio._balance_time -= 100
        assert m.portfolio.snapshot()["equity"] is None
        await m.portfolio.close()
    asyncio.run(scenario())


def test_refresh_in_flight_does_not_make_old_balances_fresh(tmp_path):
    async def scenario():
        m = manager(tmp_path, mode="testnet", binance_api_key="test", binance_api_secret="test")
        m.portfolio._balances = {"balances": [{"asset": "USDT", "free": "1000", "locked": "0"}]}
        m.portfolio._balance_time = time.monotonic() - 100
        gate = asyncio.Event()
        async def slow():
            await gate.wait()
        m.primary_bot.exchange.account = slow
        assert await m.portfolio.poll_equity() is None
        assert await m.portfolio.poll_equity() is None
        await m.portfolio.close()
    asyncio.run(scenario())


def test_portfolio_guard_enforces_allowlist_exposure_and_cash(tmp_path):
    async def scenario():
        m = manager(tmp_path)
        submit = AsyncMock(return_value=ExecutionResult("BUY", True, "test"))
        result = await m.portfolio.execute_entry("DOGEUSDT", submit, notional=10)
        assert not result.success and "configured" in result.message
        result = await m.portfolio.execute_entry("BTCUSDT", submit, notional=200)
        assert not result.success and "exposure" in result.message
        result = await m.portfolio.execute_entry("BTCUSDT", submit, notional=1100)
        assert not result.success and "balance" in result.message
        submit.assert_not_awaited()
    asyncio.run(scenario())


def test_concurrent_symbols_cannot_exceed_combined_exposure(tmp_path):
    async def scenario():
        m = manager(tmp_path, max_portfolio_exposure_fraction=.075)
        async def buy(symbol):
            bot = m.get_bot(symbol)
            await tick(bot, 100)
            bot.exchange.normalize_quantity = AsyncMock(side_effect=lambda s, q, p: q)
            bot.exchange.normalize_price = AsyncMock(side_effect=lambda s, p: p)
            return await m.portfolio.execute_entry(symbol, lambda: bot.broker.enter(
                StrategySignal(SignalSide.BUY, .95, "test"), RiskDecision(True, "test", .5), 100),
                notional=50)
        results = await asyncio.gather(buy("BTCUSDT"), buy("ETHUSDT"))
        assert sum(result.success for result in results) == 1
        assert m.portfolio.snapshot()["exposure"] == 50
        assert m.portfolio.snapshot()["available_quote"] == pytest.approx(949.93999)
    asyncio.run(scenario())


def test_exit_of_one_coin_does_not_remove_another_coin(tmp_path):
    async def scenario():
        m = manager(tmp_path)
        btc, eth = m.get_bot("BTCUSDT"), m.get_bot("ETHUSDT")
        position(btc, 1)
        position(eth, 2)
        await btc.broker.maybe_exit(StrategySignal(SignalSide.HOLD, .5, "test"), 94)
        assert btc.broker._paper_base_dec() == 0
        assert eth.broker._paper_base_dec() == 2
    asyncio.run(scenario())


def test_bad_ticks_cannot_replace_a_good_price(tmp_path):
    async def scenario():
        bot = manager(tmp_path).primary_bot
        await tick(bot, 100)
        timestamp = bot.latest_price_event_ms
        for price, at in [(90, timestamp - 1), (float("nan"), timestamp), (-1, timestamp), (200, timestamp + 60000)]:
            await bot._on_price_tick(price, at)
            assert bot.latest_price == 100
    asyncio.run(scenario())


def test_entry_quality_expiry_deviation_and_cooldown(tmp_path):
    bot = manager(tmp_path).primary_bot
    candidate = {"candle_close_time": int(time.time() * 1000) - 1000, "signal_price": 100}
    assert bot._entry_quality_reason(candidate, 100) is None
    assert "too far" in bot._entry_quality_reason(candidate, 105)
    assert "expired" in bot._entry_quality_reason({**candidate, "candle_close_time": 123}, 100)
    trade_id = position(bot)
    bot.db.close_trade(trade_id, 94, 0, "Stop loss")
    assert "cooldown" in bot._entry_quality_reason(candidate, 100)


def test_adaptation_reduces_exposure_capped_size_and_costs_are_budgeted():
    settings = Settings(_env_file=None)
    risk = RiskManager(settings)
    args = dict(signal=StrategySignal(SignalSide.BUY, .95, "test"), price=100,
                equity=1000, daily_realized_pnl=0, threshold=.7)
    full = risk.evaluate_entry(**args, risk_multiplier=1)
    reduced = risk.evaluate_entry(**args, risk_multiplier=.5)
    assert reduced.quantity == pytest.approx(full.quantity / 2)
    costs = 1 + (settings.trading_fee_bps + settings.paper_slippage_bps) / 10000
    assert full.quantity * 100 * costs <= 50 + 1e-9
    assert not risk.evaluate_entry(**{**args, "price": float("nan")}, risk_multiplier=1).allowed


def test_breakeven_stop_includes_entry_exit_costs(tmp_path):
    async def scenario():
        bot = manager(tmp_path).primary_bot
        bot.db.open_trade(mode="paper", symbol="BTCUSDT", quantity=1, entry_price=100,
                          entry_fee=.1, reason="test", stop_price=95, take_profit_price=110,
                          state_updates={bot.broker._paper_base_key: 1})
        bot.exchange.normalize_price = AsyncMock(side_effect=lambda s, p: p)
        await bot.broker.maybe_exit(StrategySignal(SignalSide.HOLD, .5, "test"), 100.7)
        trade = bot.db.get_open_trade("BTCUSDT", "paper")
        result = await bot.broker.maybe_exit(StrategySignal(SignalSide.HOLD, .5, "test"), trade["stop_price"])
        assert result.success
        assert result.details["pnl"] == pytest.approx(0, abs=1e-10)
    asyncio.run(scenario())


def test_all_four_coins_run_through_engine_with_shared_position_limit(tmp_path):
    async def scenario():
        m = manager(tmp_path)
        now = int(time.time() * 1000)
        for bot in m.bots.values():
            bot._exchange_validated = True
            await tick(bot, 100)
            bot._compute_strategy = AsyncMock(return_value={
                "signal": StrategySignal(SignalSide.BUY, .95, "test"),
                "candle_close_time": now - 1000, "signal_price": 100,
                "llm_adjustment": 0, "llm_reason": "disabled"})
            bot.exchange.normalize_quantity = AsyncMock(side_effect=lambda s, q, p: q)
            bot.exchange.normalize_price = AsyncMock(side_effect=lambda s, p: p)
            bot.exchange.market_order = AsyncMock(side_effect=AssertionError("No network orders"))
        results = await asyncio.gather(*(bot.run_once() for bot in m.bots.values()))
        assert {result["symbol"] for result in results} == set(m.symbols)
        assert sum(result["execution"]["action"] == "BUY" for result in results) == 2
        assert m.portfolio.db.count_open_trades(mode="paper") == 2
        assert m.portfolio.snapshot()["exposure_fraction"] <= .15
        for bot in m.bots.values():
            bot.exchange.market_order.assert_not_awaited()
        # Restart retains shared portfolio and independent holdings.
        restarted = manager(tmp_path)
        for bot in restarted.bots.values():
            await tick(bot, 100)
        assert restarted.portfolio.snapshot()["equity"] == pytest.approx(m.portfolio.snapshot()["equity"])
    asyncio.run(scenario())


def test_unresolved_order_on_one_coin_blocks_another_coin(tmp_path):
    async def scenario():
        m = manager(tmp_path)
        m.portfolio.db.record_order_intent(mode="paper", symbol="ETHUSDT",
            client_order_id="unknown-test", side="BUY", requested_quantity=1)
        submit = AsyncMock()
        result = await m.portfolio.execute_entry("BTCUSDT", submit, notional=10)
        assert not result.success
        assert "ETHUSDT" in result.message
        submit.assert_not_awaited()
    asyncio.run(scenario())


def test_failed_account_refresh_blocks_new_allocations(tmp_path):
    async def scenario():
        m = manager(tmp_path, mode="testnet", binance_api_key="test", binance_api_secret="test")
        m.primary_bot.exchange.account = AsyncMock(side_effect=RuntimeError("Offline"))
        await m.portfolio.poll_equity()
        await asyncio.gather(m.portfolio._balance_task, return_exceptions=True)
        assert await m.portfolio.poll_equity() is None
        assert m.portfolio.snapshot()["error"] == "Offline"
        submit = AsyncMock()
        result = await m.portfolio.execute_entry("BTCUSDT", submit, notional=10)
        assert not result.success
        submit.assert_not_awaited()
        await m.portfolio.close()
    asyncio.run(scenario())


def test_account_holdings_outside_bot_positions_still_count_toward_limit(tmp_path):
    async def scenario():
        m = manager(tmp_path, mode="testnet", binance_api_key="test", binance_api_secret="test")
        await tick(m.primary_bot, 100)
        m.portfolio._balances = {"balances": [
            {"asset": "USDT", "free": "100", "locked": "0"},
            {"asset": "BTC", "free": "9", "locked": "0"}]}
        m.portfolio._balance_time = time.monotonic()
        snapshot = m.portfolio.snapshot()
        assert snapshot["equity"] == 1000
        assert snapshot["managed_exposure"] == 0
        assert snapshot["unmanaged_exposure"] == 900
        assert snapshot["holdings"][0]["unmanaged_quantity"] == 9
        submit = AsyncMock()
        result = await m.portfolio.execute_entry("BTCUSDT", submit, notional=10)
        assert not result.success
        assert "90.0%" in result.message and "15.0%" in result.message
        assert result.details["order_submitted"] is False
        submit.assert_not_awaited()
        m.primary_bot.latest_price_event_ms -= 60000
        stale = m.portfolio.snapshot()
        assert stale["equity"] is None
        assert stale["exposure"] is None
        assert stale["unmanaged_exposure"] is None
        assert stale["holdings"] == []
    asyncio.run(scenario())


def record_close(m, symbol, exit_price):
    bot = m.get_bot(symbol)
    trade_id = position(bot)
    bot.db.close_trade(trade_id, exit_price, 0, "test")
    return trade_id


def test_portfolio_loss_pause_blocks_entries_and_survives_restart(tmp_path):
    async def scenario():
        m = manager(tmp_path)
        for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
            record_close(m, symbol, 99)
        assert "3 consecutive losses" in m.portfolio.loss_pause_reason()
        restarted = manager(tmp_path)
        submit = AsyncMock()
        result = await restarted.portfolio.execute_entry("XRPUSDT", submit, notional=10)
        assert not result.success and "Loss-streak pause" in result.message
        submit.assert_not_awaited()
        # Existing positions can still exit during the entry pause.
        xrp = m.get_bot("XRPUSDT")
        position(xrp)
        result = await xrp.broker.maybe_exit(StrategySignal(SignalSide.HOLD, .5, "test"), 94)
        assert result.success and result.action == "SELL"
    asyncio.run(scenario())


def test_loss_pause_expires_and_a_nonlosing_close_breaks_streak(tmp_path):
    from datetime import datetime, timedelta, timezone
    m = manager(tmp_path)
    for symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        record_close(m, symbol, 99)
    with m.portfolio.db.connection() as conn:
        conn.execute("UPDATE trades SET closed_at=?", (
            (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
    assert m.portfolio.loss_pause_reason() is None
    record_close(m, "XRPUSDT", 99)
    assert m.portfolio.loss_pause_reason() is not None
    record_close(m, "BTCUSDT", 101)
    assert m.portfolio.loss_pause_reason() is None


def test_loss_pause_uses_close_order_and_filters_mode_and_symbols(tmp_path):
    m = manager(tmp_path, max_consecutive_losses=2)
    # Older entry closes last with a win, breaking the more recent entries' losses.
    winner = position(m.primary_bot)
    record_close(m, "ETHUSDT", 99)
    record_close(m, "SOLUSDT", 99)
    m.primary_bot.db.close_trade(winner, 101, 0, "test")
    assert m.portfolio.loss_pause_reason() is None
    recent = m.portfolio.db.recent_portfolio_closes(mode="paper", symbols=["BTCUSDT"], limit=2)
    assert [trade["id"] for trade in recent] == [winner]
    assert m.portfolio.db.recent_portfolio_closes(mode="live", symbols=m.symbols, limit=2) == []
