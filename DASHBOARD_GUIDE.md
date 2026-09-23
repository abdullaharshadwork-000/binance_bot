# Dashboard Guide

## Buttons

- **Analyze Now (blue):** refreshes the completed-candle strategy immediately and runs one market/risk cycle.
- **Start Bot (green):** starts the real-time price stream and the automatic 1-second risk monitor.
- **Stop Bot (red):** stops automatic market monitoring and order decisions.
- **Refresh (gray):** refreshes dashboard information only.

## Market Feed

- **LIVE / green:** fresh Binance WebSocket price data is arriving.
- **FALLBACK / amber:** WebSocket data is not fresh; the engine is using REST price fallback.
- **STOPPED:** the automated engine is not running.

The dashboard shows price-feed age in milliseconds so you can see whether the displayed price is fresh.

## Two different clocks

### Market / risk clock — default 1 second
Every second the engine checks the latest price. If a position is open, it checks stop loss and take profit immediately on that cycle.

### Strategy clock — default 15 minutes
BUY/HOLD/SELL indicators use completed candles. A new strategy decision is calculated when a new completed candle exists. This prevents hundreds of duplicate decisions from one unfinished 15-minute candle.

## Risk Engine

A BUY signal does not automatically place an order. The risk engine must also approve confidence, daily loss limits, exposure, and position size.

## Precision

The bot uses decimal arithmetic for critical money/risk calculations and normalizes order quantity and prices to Binance symbol rules before execution.

## Paper realism

Paper mode includes both trading fees and configurable slippage. This deliberately makes simulated results less optimistic than assuming perfect fills.
