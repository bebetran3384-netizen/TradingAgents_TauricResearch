"""Run TradingAgents on each watchlist symbol and read back a tradeable signal."""

from __future__ import annotations

import re

from tradingbot.planner import Signal

_LEVEL_RE = r"\*\*{label}\*\*:\s*\$?([0-9][0-9,]*\.?[0-9]*)"


def product_id(symbol: str, asset_type: str) -> str:
    """Coinbase product for a watchlist symbol: stocks trade as TICKER-USD."""
    symbol = symbol.upper()
    if asset_type == "crypto":
        return symbol if "-" in symbol else f"{symbol}-USD"
    return f"{symbol}-USD"


def parse_level(text: str, label: str) -> float | None:
    """Read "**Entry Price**: 23.85" from the trader's rendered proposal."""
    m = re.search(_LEVEL_RE.format(label=re.escape(label)), text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def last_price(symbol: str) -> float | None:
    import yfinance as yf

    hist = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
    if hist.empty:
        return None
    return float(hist["Close"].iloc[-1])


def run_signal(symbol: str, asset_type: str, trade_date: str, cfg: dict) -> Signal:
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = DEFAULT_CONFIG.copy()
    for key in ("llm_provider", "deep_think_llm", "quick_think_llm"):
        if cfg.get(key):
            config[key] = cfg[key]
    analysts = ["market", "social", "news"]
    if asset_type != "crypto":
        analysts.append("fundamentals")

    graph = TradingAgentsGraph(selected_analysts=analysts, config=config)
    state, rating = graph.propagate(symbol, trade_date, asset_type=asset_type)
    plan = state.get("trader_investment_plan", "")
    return Signal(
        symbol=symbol,
        asset_type=asset_type,
        product_id=product_id(symbol, asset_type),
        rating=rating,
        price=last_price(symbol) or 0.0,
        entry=parse_level(plan, "Entry Price"),
        stop=parse_level(plan, "Stop Loss"),
    )
