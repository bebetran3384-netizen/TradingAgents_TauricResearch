"""``tradingagents-bot``: run the trading bot once or on a daily schedule.

    tradingagents-bot                 # sleep until TRADING_BOT_RUN_TIME each weekday, then run
    tradingagents-bot --once          # run now for today's date
    tradingagents-bot --date 2026-10-27   # run once for a specific date
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta

import pytz
from dotenv import load_dotenv

from tradingagents.bot.config import BotConfig, BotConfigError
from tradingagents.bot.runner import run_once

logger = logging.getLogger("tradingagents.bot")


def next_run_at(now: datetime, hour: int, minute: int) -> datetime:
    """Next weekday at ``hour:minute`` strictly after ``now`` (tz-aware, same zone)."""
    tz = now.tzinfo
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    # Re-localize so a DST change between now and the run keeps the wall-clock time.
    if hasattr(tz, "localize"):
        candidate = tz.localize(candidate.replace(tzinfo=None))
    return candidate


def _run_and_log(cfg: BotConfig, trade_date: date) -> None:
    try:
        entries = run_once(cfg, trade_date)
    except Exception:
        logger.exception("Bot run for %s failed", trade_date)
        return
    for e in entries:
        logger.info("%s %s %s: %s", e["date"], e["ticker"], e["status"], e.get("detail", ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradingagents-bot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="run one pass now and exit")
    parser.add_argument("--date", type=date.fromisoformat,
                        help="trade date (YYYY-MM-DD) for a single pass; implies --once")
    args = parser.parse_args(argv)

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = BotConfig.from_env()
    except BotConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 2

    tz = pytz.timezone(cfg.timezone)
    logger.info(
        "Mode=%s watchlist=%s max_position=$%.0f max_exposure=$%.0f",
        cfg.mode, ",".join(cfg.watchlist), cfg.max_position_usd, cfg.max_total_exposure_usd,
    )
    if cfg.mode == "live":
        logger.warning("LIVE MODE: orders will be placed with real money")

    if args.once or args.date:
        _run_and_log(cfg, args.date or datetime.now(tz).date())
        return 0

    hour, minute = cfg.run_hour_minute
    while True:
        target = next_run_at(datetime.now(tz), hour, minute)
        logger.info("Next run at %s", target.isoformat())
        while (remaining := (target - datetime.now(tz)).total_seconds()) > 0:
            time.sleep(min(remaining, 300))
        _run_and_log(cfg, target.date())


if __name__ == "__main__":
    sys.exit(main())
