# Dashboard Guide

## Market universe

The watchlist can use a configured symbol list or a dynamic `top_volume` universe.
Dynamic mode ranks eligible Binance Spot pairs by rolling 24-hour quote turnover,
excludes stablecoin and leveraged-token bases, and keeps up to
`UNIVERSE_MAX_SYMBOLS`. In Testnet mode, a pair must also exist in Testnet's
supported Spot symbol list; its 24-hour rank uses public live Spot turnover, while
candles, prices, and orders remain on Testnet. `UNIVERSE_MIN_QUOTE_VOLUME` filters
thin markets.
`SYMBOLS` and `UNIVERSE_PINNED_SYMBOLS` add manual inclusions. Open positions and
unresolved orders stay tracked when a pair drops out of the ranking.

Use **Discover top markets** while trading is stopped to refresh the list. **Start
trading** forces another refresh before starting the engines. The watchlist supports
search, signal/position filters, liquidity/score/change sorting, and clicking a row
to focus that market. Scan all markets is read-only.

More markets do not guarantee more profit. The universe is a liquidity ranking,
not a recommendation to buy every pair. Portfolio exposure and concurrent-position
limits continue to apply across the selected watchlist.

## Controls

- **Analyze market:** refreshes completed-candle analysis for the focused market; it does not place an order.
- **Scan all markets:** performs read-only analysis across the loaded watchlist.
- **Start trading:** starts the market-price feeds and automated strategy/risk cycles for the selected universe.
- **Stop trading:** stops software monitoring and new order decisions. Exchange-resident OCO orders may remain active for non-paper positions.
- **Refresh dashboard:** reloads the displayed status and portfolio information.

## Market feed

- **LIVE:** fresh Binance WebSocket data is arriving.
- **FALLBACK:** the WebSocket feed is stale and the engine is using a recent REST price.
- **STOPPED:** the automated engine is not running.

The dashboard displays feed age and source. Stale portfolio valuations block new entries.

## Two clocks

- **Market / risk clock:** the default is one second. Open positions are checked for software-managed exits on each cycle.
- **Strategy clock:** the default is 15 minutes. Strategy signals use completed candles, not the still-forming candle.

The entry strategy combines EMA trend, RSI, momentum, relative volume, ATR extension,
ADX trend strength, and directional pressure. Its score is a heuristic—not a win
probability or proof of profitability. Protective exits remain independent of entry
filters.

## Risk and execution

A BUY signal still needs portfolio/risk approval. Controls include position sizing,
daily realized-loss checks, exposure and concurrency caps, entry-price deviation,
order-book liquidity checks, Binance filters, and order recovery. All non-quote
account balances are included when a direct/inverse Spot pair can value them in the
configured quote asset; an unpriceable asset blocks new entries rather than being
silently ignored. Paper mode includes configurable fees and slippage. Testnet/live
exchange fills can differ from estimates; loss controls do not guarantee a maximum
loss.

Keep the control dashboard on localhost. Bot-control routes reject non-loopback
clients, non-local Host headers, and cross-origin browser requests as defense in
depth; this is not authentication and does not make public/shared-network exposure
safe. Live trading requires separate explicit configuration gates and sustained
Testnet validation; passing unit tests alone is not live readiness.
