# Live trading readiness

Status: exchange-resident OCO protection and restart reconciliation are implemented
in the shared Testnet/live execution path. Live execution remains gated behind
ALLOW_LIVE_TRADING=true and LIVE_PROTECTION_VALIDATED=true until the same path is
validated against Binance Spot Testnet failure scenarios. No winning-trade guarantee is possible. The
existing strategy score is a heuristic, not a measured probability of profit.

## Implemented and tested

- Order identity checks bind symbol, side and client order ID to the request.
- Invalid prices, quantities and non-BUY entry signals cannot place an entry.
- Persisted intents and transactional checks prevent a second pending order for
  the same symbol. A timeout is queried by client ID rather than resubmitted.
- A filled order and its position change are marked applied atomically. Failures
  during entry/exit persistence leave a durable block against duplicate orders.
- Incomplete fill details and still-active partial fills require recovery. Repeated
  restarts do not clear `RECOVERY_REQUIRED` or create an invented local position.
- LOT_SIZE and MARKET_LOT_SIZE constraints are both enforced, including a market
  maximum when its step is disabled. Notional market-application flags are honored.
- Metadata refreshes after 60 seconds. Market orders require a trading Spot symbol
  supporting MARKET orders. The client itself enforces the paper/live order gate.
- 429/418 responses establish a process-wide cooldown across symbol clients using
  the same exchange URL, based on Retry-After. Order writes are never auto-retried.
- Non-paper entries inspect spread and visible depth, rejecting stale book reads,
  insufficient depth, crossed books and excessive estimated price movement.
- Fee/slippage sizing, portfolio exposure caps, stale-data guards, loss-streak
  pauses and the existing protective exit checks remain active.

Regression tests inject timeouts, cancellation, mismatched responses, partial
fills, persistence failures, concurrent brokers and rate limits. They use fake
exchange responses and temporary databases; they do not place exchange orders.

## Remaining blockers and limitations

1. Hard stop-loss and take-profit protection now use Binance Spot OCO order lists
   in both Testnet and live-capable code. Live execution remains gated until the
   OCO lifecycle, restart recovery, cancellation races, and failure handling are
   validated on Spot Testnet. Software trailing/breakeven exits remain an additional
   layer and cancel/reconcile the OCO before a market exit.
2. No substantial out-of-sample or forward-trading evidence demonstrates an edge
   after fees, spread and slippage. Three historical losing trades are insufficient
   to select or validate parameters. Test filters on unseen data and different
   market regimes; do not tune them solely to those three outcomes.
3. The pre-entry book check is a snapshot, not a guaranteed market-order price.
   Local notional checks also approximate exchange rules that use average prices.
4. Recovery intentionally blocks when fill/commission or position state is
   uncertain. It does not yet offer an automated, verified repair workflow.
5. Run one application process per account/database. The order-intent transaction
   protects same-symbol writes, but portfolio allocation locks and API cooldowns
   are in-process and do not coordinate multiple processes or other trading apps.
6. The database migration preserves pre-update order records as legacy applied
   records; it cannot prove their old fill-to-position mapping. Reconcile existing
   exchange holdings and order history before considering a live migration.
7. The dashboard is intended for localhost. It lacks authentication suitable for
   public or shared-network deployment and must not be exposed as a live control API.

## Verification before real funds

Keep using Binance Spot Testnet. Exercise restart/network-loss recovery, OCO
placement/cancellation, partial fills, locked balances, and account reconciliation,
then collect a documented forward-test sample and untouched historical holdout results. Define tolerable drawdown and operating limits before testing.
Passing unit tests alone is not a reason to enable ALLOW_LIVE_TRADING.

API behavior references:
- https://developers.binance.com/en/docs/products/spot/rest-api
- https://developers.binance.com/en/docs/products/spot/filters


## Additional hardening

- A cross-platform runtime file lock prevents two application processes from
  controlling the same database at once.
- SQLite enforces at most one OPEN trade per mode/symbol.
- Terminal Binance orders with complete account fills can be recovered and
  applied after a restart; incomplete or inconsistent recovery data remains
  blocked.
- Exchange/local position quantity drift is detected and blocks trading instead
  of silently closing a different quantity.
- Read-only analysis uses the same portfolio equity/capacity/exposure gates as
  live execution logic.
- Failed entries use a retry backoff instead of retrying every fast cycle.
- The optional LLM advisor can only reduce confidence; it cannot promote an
  otherwise non-qualifying trade.
- Trading-control HTTP endpoints are restricted to localhost clients.


## Testnet exchange-resident protection

The bot now installs a Binance Spot OCO protection list immediately after a
successful Testnet BUY. The upper leg is a LIMIT_MAKER take-profit and the lower
leg is a STOP_LOSS. Binance cancels the sibling leg when one executes. The local
trade persists both the Binance order-list id and list client id, and restart
reconciliation queries the order list and individual child orders before
applying a protective fill locally.

Software exits first reconcile the OCO, then cancel it, then submit a market
SELL. If an OCO leg fills during the cancellation race, the exchange fill wins
and no second SELL is submitted.

Live mode is still fail-closed. The next release gate is sustained Spot Testnet
validation of OCO placement, restart recovery, cancellation races, partial fills,
API timeouts, and locked-balance behavior.
