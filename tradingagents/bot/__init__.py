"""Scheduled trading bot that turns TradingAgents ratings into broker orders.

Runs the full TradingAgents pipeline over a watchlist once per trading day
(after the close by default) and rebalances each position toward a target
size derived from the Portfolio Manager's 5-tier rating. Orders go to Alpaca;
the default ``dry_run`` mode never sends anything.
"""

from tradingagents.bot.config import BotConfig
from tradingagents.bot.policy import TARGET_WEIGHTS, OrderPlan, plan_order, protective_stop
from tradingagents.bot.runner import Analysis, run_once
