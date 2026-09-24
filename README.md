# Agentic Binance Bot v3

A conservative Binance Spot trading bot with a simple dashboard, paper/testnet/live modes, deterministic risk controls, rule-based adaptation, and fast real-time market monitoring.

## What changed in v3

- Real-time Binance WebSocket price stream while the engine is running.
- Stop-loss / take-profit monitoring every `CYCLE_SECONDS` (default: 1 second).
- Strategy signals are recalculated only from completed candles (default: 15m), not every second.
- REST price fallback if the WebSocket feed becomes stale.
- Critical risk, fee and paper-balance math uses Python `Decimal`.
- Order quantity is normalized to Binance `LOT_SIZE` / minimum notional rules.
- Stop and take-profit prices are normalized to the symbol `PRICE_FILTER` tick size.
- Paper mode includes configurable fee and slippage assumptions.
- Persistent HTTP client reduces repeated connection setup overhead.
- Live/testnet account balance refresh is throttled separately from price monitoring.
- Dashboard refreshes every second and shows market-feed freshness/source.
- Optional multi-symbol Testnet engine can run BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT (or another shared-quote list) in parallel.
- Portfolio entry coordination limits simultaneous positions and applies the daily realized-loss check across configured symbols.
- Restart-safe high-water marks, breakeven protection, and ratcheting trailing stops lock in gains without ever loosening the original stop.

## Why the strategy does not recalculate every second

While the engine is running, candle fetching and optional LLM advice run in a background task so slow or failed strategy requests do not block subsequent price-based exit checks. Each cycle checks existing positions before strategy processing, learning updates, and balance refreshes. Strategy errors appear in `last_cycle.strategy_error`, and failed refreshes cannot authorize new entries. Manual run-once checks existing exits first, then waits for its strategy result.

A 15-minute strategy should not produce 900 separate decisions from the same candle. The engine watches the live price every second for risk exits, but opens a new strategy decision only when a new completed strategy candle is available. This reduces duplicate entries and unfinished-candle noise.

## Recommended first configuration

```env
MODE=paper
ALLOW_LIVE_TRADING=false
SYMBOL=BTCUSDT
BASE_ASSET=BTC
QUOTE_ASSET=USDT

# Leave blank for single-symbol mode.
# For multi-symbol Testnet testing:
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT
MAX_CONCURRENT_POSITIONS=2
ALLOW_MULTI_SYMBOL_LIVE=false

INTERVAL=15m
CYCLE_SECONDS=1
USE_WEBSOCKET_MARKET_DATA=true
MARKET_DATA_STALE_SECONDS=3
ACCOUNT_REFRESH_SECONDS=5

PAPER_STARTING_BALANCE=1000
RISK_PER_TRADE=0.0025
MAX_POSITION_FRACTION=0.05
MAX_DAILY_LOSS_FRACTION=0.01
STOP_LOSS_PCT=0.012
TAKE_PROFIT_PCT=0.024
ENABLE_TRAILING_STOP=true
BREAKEVEN_ACTIVATION_PCT=0.006
TRAILING_STOP_ACTIVATION_PCT=0.01
TRAILING_STOP_DISTANCE_PCT=0.008
MIN_SIGNAL_CONFIDENCE=0.70
TRADING_FEE_BPS=10
PAPER_SLIPPAGE_BPS=2

ENABLE_ADAPTIVE_LEARNING=true
ALLOW_ADAPTIVE_LIVE=false
MIN_TRADES_FOR_LEARNING=50
ENABLE_LLM_ADVISOR=false
```

## Install

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m pytest
python run.py
```

Open `http://127.0.0.1:8000`.

## How adaptation works

The learning component uses fixed rules over recent closed trades to adjust the entry confidence threshold and risk multiplier. It does not train a predictive model or rewrite the strategy. Poor results tighten the threshold and reduce sizing; stronger results can restore sizing up to the configured baseline. Adaptation in live mode requires `ALLOW_ADAPTIVE_LIVE=true`.

## Trading modes

- `paper`: real market data; simulated fills.
- `testnet`: Binance Spot Testnet orders.
- `live`: real Binance Spot orders. Real orders remain blocked unless `ALLOW_LIVE_TRADING=true`.

Never give the API key withdrawal permission. Keep `.env` private.

## Multi-symbol mode

Set `SYMBOLS` to a comma-separated list of Spot symbols that share `QUOTE_ASSET`. Each symbol gets its own strategy state, WebSocket price monitor, open-position record, order reconciliation, and learning profile. The manager runs them concurrently, while `MAX_CONCURRENT_POSITIONS` prevents every qualifying signal from opening at once.

Multi-symbol live trading has a separate `ALLOW_MULTI_SYMBOL_LIVE` safety gate and should remain disabled until the Testnet behavior has been reviewed over a meaningful sample of trades. More markets increase the number of opportunities and the number of ways to lose; they do not guarantee higher profit.

## Important

Fast monitoring reduces avoidable software delay, but it does not guarantee a profitable fill or eliminate network latency, exchange latency, slippage, gaps, or losses. Paper results can differ from live results.

Trailing protection is evaluated by the running bot and is not an exchange-resident stop order. After the configured activation gain, the persisted stop follows the highest observed price at the configured distance; after the earlier breakeven activation it cannot fall below entry. Keep the process running and validate all parameters in paper and Testnet modes before considering live use.
