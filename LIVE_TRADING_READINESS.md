# Live trading readiness

Status: execution safeguards strengthened; not certified for unattended live use.
No winning-trade guarantee is possible. The existing strategy score is a heuristic,
not a measured probability of profit. This update does not enable live trading.

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

1. Stops and take-profit exits are local software checks. They cannot protect a
   position while the machine, network or process is down. Exchange-held protective
   orders, their lifecycle and restart reconciliation still need implementation
   and Testnet failure testing before unattended live use.
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

Keep using paper/Testnet. Exercise restart/network-loss recovery and partial fills,
reconcile positions and fees with exchange history, implement exchange-held
protection, and collect a documented forward-test sample and untouched historical
holdout results. Define tolerable drawdown and operating limits before testing.
Passing unit tests alone is not a reason to enable ALLOW_LIVE_TRADING.

API behavior references:
- https://developers.binance.com/en/docs/products/spot/rest-api
- https://developers.binance.com/en/docs/products/spot/filters
