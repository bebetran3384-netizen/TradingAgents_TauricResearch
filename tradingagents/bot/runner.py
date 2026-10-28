"""One trading-day pass of the bot: analyse each ticker, then trade toward target."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

from tradingagents.bot.broker import AlpacaBroker, Broker, BrokerError, DryRunBroker
from tradingagents.bot.config import BotConfig
from tradingagents.bot.policy import plan_order

logger = logging.getLogger(__name__)

# Journal statuses that mean "this ticker is finished for the day". Errors are
# not final, so re-running the same date retries only the tickers that failed.
_FINAL_STATUSES = {"submitted", "dry_run", "no_trade"}

Analyzer = Callable[[str, str], str]


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

    def analyze(ticker: str, trade_date: str) -> str:
        _, rating = graph.propagate(ticker, trade_date)
        return rating

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
    positions = broker.position_values()
    analyze = analyze or make_analyzer()

    # Orders placed after the close only fill at the next open, so positions
    # do not reflect them yet; track this run's buys to keep the caps honest.
    committed_buys = 0.0
    entries = []
    for ticker in pending:
        base = {"date": day, "ticker": ticker, "mode": cfg.mode}
        try:
            rating = analyze(ticker, day)
        except Exception as exc:
            logger.exception("Analysis failed for %s", ticker)
            entries.append(journal.record(**base, status="error", detail=f"analysis: {exc}"))
            continue

        current = positions.get(ticker, 0.0)
        exposure = sum(v for v in positions.values() if v > 0) + committed_buys
        plan, explanation = plan_order(
            ticker,
            rating,
            current,
            max_position_usd=cfg.max_position_usd,
            min_trade_usd=cfg.min_trade_usd,
            exposure_headroom_usd=max(cfg.max_total_exposure_usd - exposure, 0.0),
            buying_power_usd=max(account.buying_power - committed_buys, 0.0),
        )
        logger.info("%s %s -> %s", ticker, day, explanation)
        base.update(rating=rating, detail=explanation)

        if plan is None:
            entries.append(journal.record(**base, status="no_trade"))
            continue

        order_fields = {"side": plan.side, "notional_usd": plan.notional_usd,
                        "close_position": plan.close_position}
        try:
            result = broker.submit(plan, client_order_id=f"ta-{day}-{ticker}")
        except BrokerError as exc:
            logger.error("Order for %s failed: %s", ticker, exc)
            base["detail"] = f"{explanation}; order: {exc}"
            entries.append(journal.record(**base, **order_fields, status="error"))
            continue

        if plan.side == "buy":
            committed_buys += plan.notional_usd
        status = "submitted" if broker.sends_orders else "dry_run"
        entries.append(journal.record(**base, **order_fields, status=status,
                                      order_id=result.get("id")))
    return entries
