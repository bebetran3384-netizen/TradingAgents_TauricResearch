"""One trading-day pass of the bot: analyse each ticker, then trade toward target."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from tradingagents.bot.broker import AlpacaBroker, Broker, BrokerError, DryRunBroker
from tradingagents.bot.config import BotConfig
from tradingagents.bot.policy import TARGET_WEIGHTS, plan_order, protective_stop

logger = logging.getLogger(__name__)

# Journal statuses that mean "this ticker is finished for the day". Errors are
# not final, so re-running the same date retries only the tickers that failed.
_FINAL_STATUSES = {"submitted", "dry_run", "no_trade"}

# Matches the "**Stop Loss**: 199.5" line rendered by render_pm_decision.
_STOP_LOSS_RE = re.compile(r"\*\*Stop Loss\*\*:\s*\$?([\d,]+(?:\.\d+)?)")


@dataclass(frozen=True)
class Analysis:
    rating: str
    stop_loss: float | None = None


Analyzer = Callable[[str, str], Analysis]


def parse_stop_loss(decision_text: str) -> float | None:
    m = _STOP_LOSS_RE.search(decision_text or "")
    return float(m.group(1).replace(",", "")) if m else None


class Journal:
    """Append-only JSONL record of every per-ticker outcome."""

    def __init__(self, path: Path):
        self.path = path

    def done(self, trade_date: str) -> set[str]:
        if not self.path.exists():
            return set()
        finished = set()
        for line in self.path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("date") == trade_date and entry.get("status") in _FINAL_STATUSES:
                finished.add(entry["ticker"])
        return finished

    def record(self, **entry) -> dict:
        entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry


def make_broker(cfg: BotConfig) -> Broker:
    if cfg.mode == "dry_run":
        return DryRunBroker(buying_power=cfg.max_total_exposure_usd)
    return AlpacaBroker(cfg.alpaca_key_id, cfg.alpaca_secret_key, paper=cfg.mode == "paper")


def make_analyzer() -> Analyzer:
    """Build the TradingAgents graph once and reuse it across the watchlist."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph(config=DEFAULT_CONFIG.copy())

    def analyze(ticker: str, trade_date: str) -> Analysis:
        final_state, rating = graph.propagate(ticker, trade_date)
        return Analysis(rating, parse_stop_loss(final_state.get("final_trade_decision", "")))

    return analyze


def run_once(
    cfg: BotConfig,
    trade_date: date,
    *,
    broker: Broker | None = None,
    analyze: Analyzer | None = None,
) -> list[dict]:
    """Run the bot for ``trade_date`` and return the journal entries it wrote."""
    day = trade_date.isoformat()
    journal = Journal(cfg.journal_path)

    if cfg.kill_switch_path.exists():
        logger.warning("Kill switch %s present; skipping %s", cfg.kill_switch_path, day)
        return []

    pending = [t for t in cfg.watchlist if t not in journal.done(day)]
    if not pending:
        logger.info("All tickers already handled for %s", day)
        return []

    broker = broker or make_broker(cfg)
    if not broker.is_trading_day(trade_date):
        logger.info("%s is not a trading day; nothing to do", day)
        return []

    account = broker.account()
    if not account.tradable:
        raise BrokerError(f"Account is not tradable (status {account.status}); aborting")
    positions = broker.positions()
    analyze = analyze or make_analyzer()

    # Orders placed after the close only fill at the next open, so positions
    # do not reflect them yet; track this run's buys to keep the caps honest.
    committed_buys = 0.0
    entries = []
    for ticker in pending:
        base = {"date": day, "ticker": ticker, "mode": cfg.mode}
        try:
            analysis = analyze(ticker, day)
        except Exception as exc:
            logger.exception("Analysis failed for %s", ticker)
            entries.append(journal.record(**base, status="error", detail=f"analysis: {exc}"))
            continue
        base.update(rating=analysis.rating, pm_stop_loss=analysis.stop_loss)
        if TARGET_WEIGHTS.get(analysis.rating) is None:
            # Hold (or an unknown rating) never trades, so skip the price lookup.
            logger.info("%s %s -> %s: no change", ticker, day, analysis.rating)
            entries.append(journal.record(**base, status="no_trade",
                                          detail=f"{analysis.rating}: no change"))
            continue

        position = positions.get(ticker)
        try:
            price = position.price if position else broker.latest_price(ticker)
        except BrokerError as exc:
            entries.append(journal.record(**base, status="error", detail=f"price: {exc}"))
            continue

        stop = protective_stop(price, analysis.stop_loss, cfg.stop_loss_pct)
        exposure = sum(p.market_value for p in positions.values() if p.qty > 0) + committed_buys
        plan, explanation = plan_order(
            ticker,
            analysis.rating,
            position.qty if position else 0.0,
            price,
            stop,
            max_position_usd=cfg.max_position_usd,
            min_trade_usd=cfg.min_trade_usd,
            exposure_headroom_usd=max(cfg.max_total_exposure_usd - exposure, 0.0),
            buying_power_usd=max(account.buying_power - committed_buys, 0.0),
        )
        logger.info("%s %s -> %s", ticker, day, explanation)
        base["detail"] = explanation

        if plan is None:
            entries.append(journal.record(**base, status="no_trade"))
            continue

        base.update(side=plan.side, qty=plan.qty, price=price, notional_usd=plan.notional_usd,
                    stop_price=stop, close_position=plan.close_position)
        held_qty = position.qty if position else 0.0
        try:
            if plan.side == "sell":
                # Open stops reserve the shares; Alpaca rejects the sell otherwise.
                broker.cancel_stops(ticker)
            result = broker.submit(plan, client_order_id=f"ta-{day}-{ticker}")
        except BrokerError as exc:
            logger.error("Order for %s failed: %s", ticker, exc)
            base["detail"] = f"{explanation}; order: {exc}"
            if plan.side == "sell":
                # The stops may already be gone; put protection back on the whole position.
                base["stop_error"] = _restore_stop(broker, ticker, held_qty, stop, day)
            entries.append(journal.record(**base, status="error"))
            continue

        if plan.side == "buy":
            committed_buys += plan.notional_usd
        elif not plan.close_position:
            base["stop_error"] = _restore_stop(broker, ticker, held_qty - plan.qty, stop, day)
        status = "submitted" if broker.sends_orders else "dry_run"
        entries.append(journal.record(**base, status=status, order_id=result.get("id")))
    return entries


def _restore_stop(broker: Broker, ticker: str, qty: float, stop: float, day: str) -> str | None:
    """Place a GTC stop on the whole shares of ``qty``; return an error message or None."""
    shares = math.floor(qty)
    if shares < 1:
        return None
    try:
        broker.place_stop(ticker, shares, stop, client_order_id=f"ta-{day}-{ticker}-stop")
    except BrokerError as exc:
        logger.error("Could not place protective stop for %s (%d shares): %s", ticker, shares, exc)
        return str(exc)
    return None
