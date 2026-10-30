# TradingAgents bot

Runs TradingAgents on a crypto watchlist, turns each rating into a dollar
target, and places the orders that move toward it, inside hard limits.

```bash
cp tradingbot/config.example.json tradingbot/config.json   # then edit
python -m tradingbot --config tradingbot/config.json             # paper account
python -m tradingbot --config tradingbot/config.json --plan-only # analyze, place nothing
python -m tradingbot --config tradingbot/config.json --robinhood # queue for Robinhood
TRADINGBOT_LIVE=yes python -m tradingbot --config tradingbot/config.json --live
```

**Ratings to targets** (`target_weights` x `base_allocation_usd`): Buy 1.0,
Overweight 0.5, Underweight 0.25, Sell 0. Hold leaves the position alone; an
unreadable rating (REVIEW) never trades. Underweight and Sell only reduce a
position, never open one.

**Limits**: `max_order_usd` per order, `max_total_exposure_usd` across the
watchlist, `cash_buffer_usd` always kept, `min_order_usd` floor, and no buy
when the price is more than `max_chase_pct` (default 3%) above the agents'
entry.

**Paper mode** (default) keeps a local account in `~/.tradingbot/paper.json`,
starting from `paper_starting_cash` (default 1000).

**Robinhood mode** (`--robinhood`) needs no API key. Before a run, Claude reads
your Robinhood account through its connector and writes
`~/.tradingbot/robinhood_account.json` (`{"cash": ..., "positions": {"BTC-USD": ...}}`).
The bot plans against it and writes the orders to
`~/.tradingbot/runs/<date>/robinhood_queue.json` without placing anything. Claude
then sends each order through the connector, and it waits for your approval in
the Robinhood app; after a buy fills, the agents' stop goes in the same way.
Stocks and crypto both work.

**Live mode** places real orders on Coinbase through the Advanced Trade API. It
needs both `--live` and `TRADINGBOT_LIVE=yes`, plus `COINBASE_API_KEY` and
`COINBASE_API_SECRET` (a CDP key with trade permission) and
`pip install coinbase-advanced-py`. Buys are market orders; when the agents give
a stop, a GTC stop-limit sell for the filled size follows. Crypto only: the SDK
cannot trade stocks, so stock orders are reported as "manual". Live mode counts
coins already in the account as positions, so a bearish rating can sell
holdings you had before the bot.

Each run writes `~/.tradingbot/runs/<date>/signals.json` and `orders.json`, and
appends every order and its result to `~/.tradingbot/journal.jsonl`.
