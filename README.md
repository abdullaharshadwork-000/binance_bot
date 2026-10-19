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
- Optional dynamic market universe ranks Binance Spot pairs by rolling 24-hour quote volume; stablecoin and leveraged-token bases are excluded, with manual pins available.
- Portfolio entry coordination limits simultaneous positions and applies the daily realized-loss check across configured symbols.
- Restart-safe high-water marks, breakeven protection, and ratcheting trailing stops lock in gains without ever loosening the original stop.

## Strategy improvements (2026-10)

- **Volatility-adaptive stops (optional, off by default):** with `USE_ATR_STOPS=true`, entry stop/target distances are scaled from the signal candle's ATR instead of fixed percentages. Distances are clamped around the configured fixed stop (half to double, plus absolute `ATR_STOP_MIN_PCT`/`ATR_STOP_MAX_PCT` bounds) and preserve the configured reward-to-risk ratio. Position sizing uses the scaled stop distance. Missing or invalid ATR data falls back to the fixed percentages.
- **Trend-flip exit:** an open position is exited once price closes below both EMAs, even if short-term momentum has not yet turned negative. This caps the previous requirement of a fully confirmed bearish trend before any strategy exit.
- **Decision logging:** every completed-candle strategy decision (side, confidence, reason, features, entry checks, score components) is persisted to the `signal_log` table for later research. It does not affect trading behavior.
- **Signal analytics report:** `python signal_report.py` joins logged BUY signals to the trades they produced and reports win rate by confidence bucket, ADX, ATR%, volume ratio, and exit reason — read-only, works on any bot database copy. See "Signal analytics report" below.
- **Structured console logging:** engine start/stop, strategy results, order submissions, entries/exits, reconciliation warnings, rate-limit cooldowns, and cycle failures are logged under `LOG_LEVEL` (default `INFO`).
- **Walk-forward research mode:** `python compare_strategy_intervals.py --walk-forward` optimizes the ADX/volume entry filters on one time fold and evaluates them untouched on the next, reporting out-of-sample results next to the current-configuration baseline. Out-of-sample results on a few folds are weak evidence; treat them as a discipline against overfitting, not a profitability proof.


## Why the strategy does not recalculate every second

While the engine is running, candle fetching and optional LLM advice run in a background task so slow or failed strategy requests do not block subsequent price-based exit checks. Each cycle checks existing positions before strategy processing, learning updates, and balance refreshes. Strategy errors appear in `last_cycle.strategy_error`, and failed refreshes cannot authorize new entries. Manual run-once checks existing exits first, then waits for its strategy result.

A 15-minute strategy should not produce 900 separate decisions from the same candle. The engine watches the live price every second for risk exits, but opens a new strategy decision only when a new completed strategy candle is available. This reduces duplicate entries and unfinished-candle noise.

## Recommended first configuration

```env
MODE=testnet
ALLOW_LIVE_TRADING=false
LIVE_PROTECTION_VALIDATED=false
SYMBOL=BTCUSDT
BASE_ASSET=BTC
QUOTE_ASSET=USDT

# Rank the top liquid Spot markets instead of limiting the bot to a fixed list.
UNIVERSE_MODE=top_volume
UNIVERSE_MAX_SYMBOLS=15
UNIVERSE_MIN_QUOTE_VOLUME=1000000
UNIVERSE_REFRESH_MINUTES=60
UNIVERSE_PINNED_SYMBOLS=
SYMBOLS=
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
USE_ATR_STOPS=true
ATR_STOP_MULTIPLIER=2.0
ENABLE_TRAILING_STOP=true
BREAKEVEN_ACTIVATION_PCT=0.006
TRAILING_STOP_ACTIVATION_PCT=0.01
TRAILING_STOP_DISTANCE_PCT=0.008
MIN_SIGNAL_CONFIDENCE=0.70
STRATEGY_MIN_ADX=16
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
# Runtime only:
python -m pip install -r requirements.txt
# Runtime + test/lint tooling (required for pytest and ruff):
python -m pip install -r requirements-dev.txt
python -m pytest
ruff check .
Copy-Item .env.example .env
python run.py
```

Before `python run.py`, add **Binance Spot Testnet** API credentials to `.env`;
`.env.example` defaults to `MODE=testnet` and intentionally contains blank keys.
To run without credentials or exchange orders, use `python run.py --paper` instead.
The sample universe scans eligible markets by 24-hour quote volume (15 maximum,
minimum 1,000,000 quote units) rather than trading only the old four-symbol list.
This broadens market coverage; it does not imply higher or guaranteed returns.

Open `http://127.0.0.1:8000`.

## How adaptation works

The learning component uses fixed rules over recent closed trades to adjust the entry confidence threshold and risk multiplier. It does not train a predictive model or rewrite the strategy. Poor results tighten the threshold and reduce sizing; stronger results can restore sizing up to the configured baseline. Adaptation in live mode requires `ALLOW_ADAPTIVE_LIVE=true`.

## Trading modes

- `paper`: real market data; simulated fills.
- `testnet`: Binance Spot Testnet orders.
- `live`: real Binance Spot orders. Execution requires `ALLOW_LIVE_TRADING=true`, `LIVE_PROTECTION_VALIDATED=true`, valid API credentials, and any applicable multi-symbol live opt-in.

Never give the API key withdrawal permission. Keep `.env` private. The dashboard
control API is intended for localhost only. Host/origin checks are defense in depth,
not authentication; do not expose it through a public or shared-network proxy.

## Market universe and multi-symbol mode

`UNIVERSE_MODE=top_volume` discovers eligible Binance Spot pairs sharing
`QUOTE_ASSET`, ranks them by rolling 24-hour quote volume, and selects up to
`UNIVERSE_MAX_SYMBOLS`. In Testnet mode, the candidates must also exist in
Testnet's Spot symbol list; ranking turnover comes from public live Spot data,
while candles, prices, and orders still use Testnet. It excludes common stablecoin
bases and leveraged-token suffixes; `UNIVERSE_MIN_QUOTE_VOLUME` sets a liquidity
floor. `SYMBOLS` and
`UNIVERSE_PINNED_SYMBOLS` can add manually selected pairs. Refresh the watchlist
from the dashboard, or the engine refreshes it before Start. Existing open
positions and unresolved orders stay tracked even if their pair leaves the ranking.
Use `UNIVERSE_MODE=configured` to retain a fixed symbol list.

The bot monitors selected markets concurrently and only enters when a completed
candle passes the strategy, liquidity, account, and portfolio checks. It does not
force a trade on every coin. A larger universe increases opportunities and
operational load; it does not promise more profit. Defaults keep the portfolio to
two simultaneous positions and 15% maximum configured-coin exposure.

All bots share one background account request and one portfolio valuation:
quote cash is counted once, and selected-coin holdings (including locked
balances in Testnet/live) are valued using fresh prices. Other non-quote account
assets are also included when an active direct or inverse Spot pair can value them
in `QUOTE_ASSET`; if any cannot be priced, new entries fail closed until the account
is reconciled. Existing selected-market holdings count toward exposure even when
they are outside bot positions. Paper holdings come from each coin's open-position
ledger instead of the old shared base balance. Existing USDT paper cash is
preserved; other quote assets use separate cash keys. Previously corrupted paper
history is not repaired.

Additional settings (defaults apply without changing an existing `.env`):

| Setting | Default | Effect |
|---|---|---|
| `UNIVERSE_MODE` | `configured` in code; `top_volume` in `.env.example` | Choose a fixed list or a rolling 24-hour quote-volume-ranked Spot universe. |
| `UNIVERSE_MAX_SYMBOLS` | `15` | Maximum dynamically ranked markets (manual pins and tracked positions are retained). |
| `UNIVERSE_MIN_QUOTE_VOLUME` | `1,000,000` | Minimum 24-hour quote turnover for dynamically selected pairs. |
| `UNIVERSE_REFRESH_MINUTES` | `60` | Minimum refresh interval while stopped; explicit dashboard refresh and Start refresh immediately. |
| `UNIVERSE_PINNED_SYMBOLS` | empty | Comma-separated manual Spot pairs to keep in addition to the volume-ranked set. |
| `UNIVERSE_EXCLUDED_BASES` | common stablecoins | Comma-separated base assets excluded from dynamic discovery. |
| `STRATEGY_MIN_ADX` | `16` | Requires additional directional-trend strength for new entries; this is a heuristic, not a profitability claim. |
| `USE_ATR_STOPS` | `false` | Scale entry stop/target distances from the signal candle ATR (volatility-adaptive). Missing/invalid ATR falls back to the fixed percentages. |
| `ATR_STOP_MULTIPLIER` | `2.0` | ATR multiple used for the stop distance when `USE_ATR_STOPS=true`. |
| `ATR_STOP_MIN_PCT` / `ATR_STOP_MAX_PCT` | `0.006` / `0.05` | Absolute clamp bounds for the ATR-scaled stop distance. |
| `LOG_LEVEL` | `INFO` | Console logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Engine start/stop, orders, entries/exits, warnings, and cycle failures are logged. |
| `MAX_PORTFOLIO_EXPOSURE_FRACTION` | `0.15` | Blocks entries that would take combined valued non-quote account exposure above 15% of portfolio equity. |
| `ACCOUNT_MAX_AGE_SECONDS` | `15` | Blocks entries when account valuation is too old; protective exits continue independently. Must be at least `ACCOUNT_REFRESH_SECONDS`. |
| `MAX_ENTRY_DEVIATION_PCT` | `0.015` | Rejects entries when price has moved more than 1.5% from the signal candle close. |
| `ENTRY_COOLDOWN_SECONDS` | `60` | Prevents immediate re-entry in a coin after its latest recorded exit. |

Entry approval serializes cash, exposure, position-count, unresolved-order, and
daily-loss checks across the portfolio. Non-paper order attempts invalidate the
account snapshot before another allocation. Failed or stale account requests
block entries, and missing prices for held coins block portfolio valuation.
Signals expire after the next strategy interval plus refresh grace; rejected
order attempts cannot retry an old candle indefinitely. Entry requests queued
behind another coin are rejected for resizing if the latest price moved by more
than 0.1% while waiting.

Risk sizing includes estimated round-trip fees/slippage. The learning risk
multiplier also reduces exposure-capped position sizes. Breakeven protection
includes recorded entry fees and estimated exit costs; actual fills can still
lose money through gaps, slippage, or estimation errors. Portfolio equity,
available cash, exposure, and valuation errors appear in the dashboard.

Run offline regression checks with `python -m pytest`. These verify execution
and accounting behavior, not strategy profitability. Backtesting and sustained
Testnet validation remain necessary before enabling live trading.

Each active market gets an independent strategy state, price stream, position record,
order reconciliation, and learning profile. Portfolio cash, exposure, unresolved-order,
and position-count checks are coordinated across the selected universe. In live
mode, a multi-symbol universe has the separate `ALLOW_MULTI_SYMBOL_LIVE` gate and
should remain disabled until Testnet behavior has been reviewed over a meaningful
sample. More markets increase both the number of opportunities and the ways to lose.

## Important

Fast monitoring reduces avoidable software delay, but it does not guarantee a profitable fill or eliminate network latency, exchange latency, slippage, gaps, or losses. Testnet results can still differ from live fills, liquidity and operational conditions.

Trailing protection is evaluated by the running bot and is not an exchange-resident stop order. After the configured activation gain, the persisted stop follows the highest observed price at the configured distance; after the earlier breakeven activation it cannot fall below entry. Keep the process running and validate all parameters in Binance Spot Testnet before considering live use.


### Entry quality and decision reporting

Entries require relative volume of at least `STRATEGY_MIN_VOLUME_RATIO` (default
0.75), a close no more than `STRATEGY_MAX_EXTENSION_ATR` (default 2.0) ATR above
the fast EMA, adequate ATR-normalized EMA separation, and ADX/directional-movement
confirmation of a rising trend. These filters apply to entries, while bearish
strategy exits and protective stops remain active. Invalid OHLCV or unavailable
latest indicators produce HOLD rather than silently evaluating an older candle.
The risk status reports the final rejection if the portfolio guard or broker blocks
an entry. These are transparent heuristics—not a validated edge or a win probability.

These are conservative heuristics, not a validated profitability improvement.
Evaluate them with real Testnet fills, fees and slippage before live use. Restart
the application to load code/configuration changes; an existing process retains
its loaded strategy. No risk limits or account balances need to be reset.


### Weak trends and consecutive losses

`STRATEGY_MIN_TREND_ATR=0.25` requires the fast EMA to exceed the slow EMA by
at least one quarter of ATR before entering. This avoids entering on barely
separated crossovers. It is a configurable heuristic, not a proven profitable rule.

After `MAX_CONSECUTIVE_LOSSES=3` closed losses across configured symbols in the
current mode, new entries pause for `LOSS_STREAK_PAUSE_SECONDS=3600` from the
latest close. This state survives restarts through trade history. A nonlosing close
breaks the streak. Protective exits continue during the pause; zero pause seconds
disables this feature. History is ordered by close time, not entry order.

Account exposure includes holdings outside recorded bot positions. The dashboard
now separates these quantities and shows that an exposure rejection submitted no
order. Zero bot positions does not mean zero account exposure. Stale valuations
are shown as unavailable rather than misleading partial totals.


### Execution hardening and live-use limits

See [LIVE_TRADING_READINESS.md](LIVE_TRADING_READINESS.md) for the verified
safeguards and remaining release blockers. Automated tests validate failure
handling; they do not establish profitable trading or certify live readiness.

Non-paper BUY orders now check visible order-book depth. `MAX_ENTRY_SPREAD_BPS`
(default 20) limits the quoted spread. `MAX_ENTRY_SLIPPAGE_BPS` (default 25)
limits the absolute difference between estimated execution price and the sizing
reference. Insufficient depth or a slow book response blocks the entry. Position
and portfolio sizing budget for this non-paper slippage allowance and fees.
Protective SELL orders are not blocked by this BUY liquidity filter. A market
order can still fill beyond its earlier estimate after the book changes.

Filled exchange orders remain unresolved until their position change is committed
in the same SQLite transaction as the order's `ledger_applied` marker. If a crash
or incomplete response prevents that transaction, new orders for that symbol stay
blocked. `RECOVERY_REQUIRED` cannot be cleared merely by restarting. Compare the
exchange order, fills, commissions and local position before any manual repair;
do not delete the intent or reset the database to bypass a block. Commissions paid
in a third asset are converted through an active direct/inverse Spot market. If
conversion is unavailable, the configured fee-rate estimate is used rather than
silently treating the fee as zero; the estimate and raw commission asset/amount
are retained in the order record. Fresh-price conversion may differ from the exact
quote value at fill time, especially during delayed recovery.


### When the bot is not entering trades

The portfolio banner reports an exposure block even while every strategy says
HOLD. Existing account coin holdings count toward exposure, including holdings
that were not opened by the bot. Zero bot positions does not override that limit.
The entry panel now explains the latest non-BUY setup instead of always showing
only a wait-for-next-candle message.

To forward-test without existing Testnet holdings, stop the existing server and run:

```powershell
python run.py --paper
```

This uses public Spot prices, simulated fills and the separate persistent database
`data/paper_validation.db`. It leaves `.env` and Testnet balances unchanged, disables
exchange orders, LLM calls and adaptive learning for this launch, and preserves
configured markets and risk limits. Open the dashboard and start the simulation.
A valid BUY setup is still required; paper mode does not force trades. Running
`python run.py` again uses the normal configuration.


### Verify the strategy calculation

HOLD no longer carries the old fixed 0.50 confidence placeholder; it has no
trade score. The decision panel lists each entry condition as PASS or BLOCKED.
BUY signals include the base score, EMA-strength and momentum contributions,
volume/crossover bonuses and volatility penalty. The displayed entry threshold
is a separate risk check. Scores are heuristic, not probabilities of winning.

Indicators and signals refresh from completed strategy candles (every five
minutes when `INTERVAL=5m`), not from elapsed runtime. EMA uses recursive
exponential smoothing; RSI and ATR use alpha 1/14 with the initial observation
as the seed. Chart providers using Wilder's initial 14-period average or a different
history length can differ. Regression tests compare EMA, RSI, ATR, momentum and
volume ratios against independent arithmetic and verify the score breakdown.


### Exchange-resident Testnet protection

Testnet BUY fills now receive a Binance Spot OCO SELL protection list: a
LIMIT_MAKER take-profit above the market and a STOP_LOSS leg below the market.
The list client ID is persisted before the request; the bot reports protection
only after Binance confirms the matching active list and both expected child IDs
with distinct order IDs. An incomplete or mismatched response is retained as
`PROTECTION_UNKNOWN` and is not blindly resubmitted. Restart reconciliation
verifies both child orders and their identities before applying complete Binance
account fills locally. A verified terminal list is replaced only after its child
states and fills are reconciled, with per-list cumulative baselines preserving
accounting when a partial exit leaves residual quantity to re-protect. An immediately
terminal placement response is reconciled before protection is reported; uncertain
or incomplete outcomes remain blocked rather than duplicating an OCO.

Software breakeven/trailing logic remains an additional layer. When it chooses
to exit earlier, the bot first reconciles and cancels the OCO before submitting
a market SELL, including the race where the OCO fills while cancellation is in
flight.

The same protection path is used by both Testnet and live exchange modes. Live order execution still requires `ALLOW_LIVE_TRADING=true` and `LIVE_PROTECTION_VALIDATED=true`; keep both live gates disabled until
the OCO lifecycle has been validated with restart, timeout, partial-fill and
network-failure scenarios on Binance Spot Testnet.


## Real Binance Spot Testnet protection validation

Use the validation harness only while the normal bot is stopped. It performs a
small real Spot Testnet round trip through the same broker code used by the
live-capable path:

1. verifies MODE=testnet and API credentials;
2. refuses to run if the normal bot owns the main database lock;
3. refuses to run if the selected symbol already has open Binance orders;
4. buys approximately the requested quote amount on Spot Testnet;
5. verifies the persisted Binance OCO list and both child orders are active;
6. recreates the exchange client and broker against the same validation database
   to simulate a restart;
7. reconciles the persisted protection after restart;
8. cancels/reconciles the OCO and sells the validation position.

PowerShell:

```powershell
python validate_testnet_protection.py --quote-amount 25
```

The validator uses `data/testnet_protection_validation.db` by default and never
uses paper fills. If any step becomes uncertain it exits non-zero instead of
pretending the validation succeeded. Check Binance Testnet open orders and balances
before retrying after a failed/aborted run.

A manual GitHub Actions workflow named **Testnet protection smoke** is also
available. It never runs on push or pull request. To use it, configure the
`testnet-validation` environment with secrets `BINANCE_TESTNET_API_KEY` and
`BINANCE_TESTNET_API_SECRET`, then manually dispatch the workflow with the
desired Testnet quote amount.


## Two-process restart validation

After the one-shot Testnet OCO validation passes, run the stricter restart test in
two separate Python processes.

Phase 1 opens a small real Spot Testnet position, verifies the exact Binance OCO,
persists its identifiers, and then exits the Python process while leaving the
position protected on Binance:

```powershell
python validate_testnet_protection.py --open-and-exit --quote-amount 25
```

Do not start the normal bot between phases. In a new PowerShell invocation, run:

```powershell
python validate_testnet_protection.py --recover-and-close
```

Phase 2 refuses to recreate missing protection. It must rediscover the exact same
`listClientOrderId` and `orderListId` persisted by phase 1. If the OCO is still
active, the validator reconciles it and cleanly closes the validation position. If
a protective leg filled while the bot was stopped, the broker reconciles that
exchange fill instead.

If either phase fails, use the conservative recovery command rather than deleting
the validation database:

```powershell
python validate_testnet_protection.py --resume
```


## Offline Binance protective-fill validation

After the two-process restart test passes, validate that Binance can close the
position while Python is completely stopped.

Open a Testnet position with a deliberately closer take-profit leg:

```powershell
python validate_testnet_protection.py --open-offline-fill --target tp --trigger-pct 0.003 --quote-amount 25
```

The command exits with the OCO active on Binance. Keep the normal bot stopped.
After Testnet price reaches the take-profit, run:

```powershell
python validate_testnet_protection.py --recover-offline-fill
```

The recovery command only passes when Binance reports the OCO is `ALL_DONE`,
the broker reconstructs the exchange protective fill, the local position closes,
and no new bot market SELL order is recorded during recovery. If the OCO is still
`EXECUTING`, the command fails without modifying or closing the position.

To test the stop-loss side separately:

```powershell
python validate_testnet_protection.py --open-offline-fill --target sl --trigger-pct 0.003 --quote-amount 25
```

Then, after the stop executes on Binance while Python remains stopped:

```powershell
python validate_testnet_protection.py --recover-offline-fill
```

The default target distance is 0.3%. Use conservative Testnet-only amounts. If a
validation is interrupted or becomes uncertain, use `--resume` instead of
deleting the validation database.


## Fast deterministic stop-loss recovery check

If there is not enough time to wait for BTC Testnet to move through the offline
stop-loss naturally, run the deterministic recovery test instead:

```powershell
python -m pytest tests/test_exchange_safety.py -k offline_stop_loss_fill_reconciles_without_duplicate_market_sell -q
```

This test drives the production broker recovery path with Binance-shaped data for
an `ALL_DONE` OCO, a canceled take-profit leg, a filled `STOP_LOSS` leg, and
matching `myTrades` fills. It fails if recovery submits another market SELL,
uses the wrong fill quantity/price, or leaves the local position open.

This is a deterministic logic/safety test. It is not a substitute for observing
a real exchange-side Testnet stop trigger, so the live-validation gate remains
unchanged until the remaining exchange/failure tests are complete.

## Read-only pre-live readiness check

Before making any live-mode decision, keep the bot in Testnet with all live gates
disabled and run:

```powershell
python check_live_readiness.py
```

The checker is intentionally read-only. It does not place, cancel, or modify
orders and does not change `.env`. It verifies the current configuration, local
unresolved-order state, Binance Testnet connectivity, signed account access, and
persisted/exchange OCO identity and both active child-order states for any local
open Testnet position. It also flags configured-symbol open orders whose client IDs do not use the bot's
`agt-` prefix.

A green result means the inspected operational state is internally consistent;
it does not prove profitability and does not automatically authorize live
trading. Keep `ALLOW_LIVE_TRADING=false`,
`LIVE_PROTECTION_VALIDATED=false`, and `ALLOW_MULTI_SYMBOL_LIVE=false` until
the remaining documented release gates have been deliberately completed.

## Testnet balance cleanup

When Binance Spot Testnet contains pre-funded BTC/ETH/SOL/XRP balances that the
bot did not open, those holdings count as unmanaged portfolio exposure and can
block new entries. With the normal bot stopped, run:

```powershell
python cleanup_testnet_balances.py
```

The utility refuses to run unless `MODE=testnet` and all live-mode gates remain
disabled. It also refuses to run if the bot runtime lock is held, if any local
managed position or unresolved order exists, if a configured asset balance is
locked, or if Binance has an open order on a configured symbol.

It first prints a normalized SELL plan and then requires the exact confirmation
`SELL TESTNET` before submitting market SELL orders. Tiny balances that do not
meet Binance filters are left as dust. If an order submission times out, the
utility queries the same client order ID and never blindly resubmits.

After cleanup, run `python check_live_readiness.py`, start the bot again, and
verify `/status` shows unmanaged exposure below `MAX_PORTFOLIO_EXPOSURE_FRACTION`.


## Forward Testnet performance report

While the normal Testnet bot continues collecting trades, summarize the closed
trade history with:

```powershell
python forward_test_report.py
```

The report includes raw PnL, win/loss counts, win rate, profit factor, maximum
drawdown, per-symbol performance, exit-reason counts, and progress toward the
adaptive-learning sample threshold.

Because Binance Spot Testnet can report zero commissions, the report also shows:

- a fee-adjusted scenario that adds only the shortfall needed to reach the
  configured `TRADING_FEE_BPS` on both entry and exit notionals; and
- a conservative stress scenario that additionally subtracts extra slippage on
  both sides of each trade.

The default extra slippage assumption is 2 bps per side. Override it without
changing trading behavior:

```powershell
python forward_test_report.py --slippage-bps 5
```

You can also inspect only the most recent closed trades:

```powershell
python forward_test_report.py --limit 20
```

These are analytical scenarios only. They do not alter the database, strategy,
orders, risk limits, or live-trading gates, and they do not establish
profitability or live readiness.


## Multi-interval historical strategy comparison

To compare the current strategy across 1m, 3m, 5m, and 15m candles without
changing Testnet or placing orders, run:

```powershell
python compare_strategy_intervals.py
```

By default it downloads seven days of public Binance Spot candle history for
all configured symbols when `UNIVERSE_MODE=configured` is active. In
`top_volume` mode, it uses the last selected universe saved by the trading engine,
plus configured symbols, manual pins, and any symbols retained for open positions
or unresolved orders. Discover the dynamic universe in the dashboard at least
once before running the comparison; otherwise the tool fails instead of silently
backtesting only the fallback symbol. The comparison evaluates the current
EMA/momentum/RSI/volume entry rules (including the configured ADX threshold), stop
loss, take profit, breakeven/trailing protection, configured fees, baseline
slippage, and an additional stress-slippage scenario.

Useful overrides:

```powershell
python compare_strategy_intervals.py --days 14
python compare_strategy_intervals.py --intervals 1m,3m,5m,15m --notional 3000
python compare_strategy_intervals.py --stress-slippage-bps 5
```

Entries are evaluated only on completed candles and are simulated at the next
candle open. Strategy exits also execute at the next candle open. Because OHLC
candles do not reveal intrabar event order, a candle that touches both stop and
take-profit is conservatively treated as a stop first, and newly ratcheted
trailing/breakeven stops apply from the next candle.

The tool is research-only. It uses public historical market data, does not use
the trading endpoints, does not mutate the bot database, and does not change the
running Testnet configuration. Historical results do not guarantee future
performance.

### Walk-forward optimization check

`compare_strategy_intervals.py --walk-forward` guards against parameter tuning
that only looks good in-sample. The historical window is split into contiguous
folds; for each fold the ADX and volume-ratio entry filters are re-selected on
the training segment by stress-adjusted PnL, then evaluated untouched on the
next segment. The report shows, per symbol and fold: chosen parameters,
in-sample PnL, out-of-sample PnL, and the same out-of-sample PnL for your
current configuration as a baseline.

Interpretation guidance:

- Optimized out-of-sample PnL consistently above the baseline suggests the
  filters carry information; the opposite suggests the defaults are fine (or
  that the grid is fitting noise).
- A handful of folds is weak evidence. Re-run across more history
  (`--days 30`), more folds (`--wf-folds 6`), and multiple intervals before
  drawing conclusions.
- The tool is research-only: it never places orders and never changes the
  running bot configuration.

### Signal analytics report

The `signal_log` table records every completed-candle decision. The read-only
analyzer attributes each closed trade to the newest BUY signal logged for the
same symbol/mode within a configurable window (default 30 minutes, matching
the live engine's fresh-candle entry discipline) and aggregates outcomes:

```powershell
python signal_report.py
python signal_report.py --symbol ETHUSDT --mode paper
python signal_report.py --database data/paper_validation.db --max-signal-age-minutes 60
```

The JSON report shows overall win rate/average PnL plus win rates bucketed by
signal confidence, ADX, signal-candle ATR%, volume ratio, and exit reason.
Use it to see which entry filters actually separate winners from losers in
your own collected history. Treat small samples skeptically: descriptive
statistics are not causation, and none of this establishes live readiness.

### Simulating and tuning ATR stops in historical research

`compare_strategy_intervals.py` now simulates the same volatility-adaptive
stops the live risk engine applies: when `USE_ATR_STOPS=true`, each simulated
entry's stop/target distances are scaled from the qualifying signal candle's
ATR using `RiskManager` (clamped, reward:risk preserved). Every recorded
trade includes the applied `stop_pct`/`take_profit_pct`, and the report's
`strategy_parameters` include the ATR settings.

The walk-forward grid also tunes stop mode: `use_atr_stops: false/true` is
optimized alongside the ADX/volume filters, so each fold reports whether the
out-of-sample winner used fixed or ATR-scaled stops:

```powershell
python compare_strategy_intervals.py --walk-forward --days 14 --wf-folds 4
```

Consistent selection of one stop mode across folds — with matching
out-of-sample performance — is meaningful evidence; flip-flopping between
modes fold to fold means the data cannot yet distinguish them.

### Monitoring and notifications

The dashboard is not the only way to watch the bot:

- **`GET /metrics`** exposes Prometheus text-format metrics: per-symbol engine
  running state, cycle counters, last cycle duration, market-feed age/staleness,
  open positions, portfolio equity, and daily realized PnL. Scrape it from a
  localhost or private-network monitor; treat it like `/status` and never
  expose the dashboard port publicly.
- **Account equity transparency**: when equity cannot be valued, the dashboard
  tile (and `status.equity_error`) states why — e.g. missing/rejected API
  credentials, a failed account request, waiting for the first refresh, or
  account data older than `ACCOUNT_MAX_AGE_SECONDS`.
- **Notifications (optional)**: set `NOTIFY_WEBHOOK_URL` (generic JSON POST:
  `{event, text, mode, symbol, timestamp}`) and/or
  `NOTIFY_TELEGRAM_BOT_TOKEN` + `NOTIFY_TELEGRAM_CHAT_ID` to receive alerts
  for executed trades, reconciliation/protection warnings, engine cycle
  errors, and start/stop events. Identical events are rate-limited to one
  message per `NOTIFY_MIN_INTERVAL_SECONDS` (default 300) per event type.
  Delivery failures are logged and never affect trading.
