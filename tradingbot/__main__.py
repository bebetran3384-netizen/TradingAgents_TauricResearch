"""Daily TradingAgents bot.

    python -m tradingbot --config tradingbot/config.json            # paper (default)
    python -m tradingbot --config tradingbot/config.json --plan-only
    TRADINGBOT_LIVE=yes python -m tradingbot --config ... --live      # real money

Live mode needs both the --live flag and TRADINGBOT_LIVE=yes, so neither a
stray flag nor a stray environment variable can spend money alone.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from tradingbot.brokers import CoinbaseBroker, PaperBroker
from tradingbot.planner import plan_orders
from tradingbot.signals import run_signal


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradingbot")
    ap.add_argument("--config", required=True)
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--plan-only", action="store_true", help="analyze and plan, place nothing")
    ap.add_argument("--live", action="store_true", help="real orders on Coinbase")
    args = ap.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text())
    state_dir = Path(os.path.expanduser(cfg.get("state_dir", "~/.tradingbot")))
    run_dir = state_dir / "runs" / args.date
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.live:
        if os.getenv("TRADINGBOT_LIVE") != "yes":
            print("Refusing live mode: set TRADINGBOT_LIVE=yes as well as --live.", file=sys.stderr)
            return 2
        broker = CoinbaseBroker(cfg.get("portfolio_id"))
    else:
        broker = PaperBroker(state_dir / "paper.json", float(cfg.get("paper_starting_cash", 1000)))

    signals = []
    for item in cfg["watchlist"]:
        print(f"Analyzing {item['symbol']} for {args.date}...", flush=True)
        try:
            signals.append(run_signal(item["symbol"], item.get("asset_type", "stock"), args.date, cfg))
        except Exception as exc:  # one bad ticker must not stop the rest
            print(f"  {item['symbol']}: analysis failed ({type(exc).__name__}: {exc})", flush=True)
    for s in signals:
        print(f"  {s.product_id}: {s.rating} at {s.price:.2f} (entry {s.entry}, stop {s.stop})")

    plan = plan_orders(signals, broker.positions(), broker.cash(), cfg)
    (run_dir / "signals.json").write_text(json.dumps([asdict(s) for s in signals], indent=2))
    (run_dir / "orders.json").write_text(json.dumps(
        {"orders": [o.to_dict() for o in plan.orders], "skipped": [asdict(k) for k in plan.skipped]},
        indent=2))

    for k in plan.skipped:
        print(f"  skip {k.product_id}: {k.reason}")
    mode = "plan only" if args.plan_only else "LIVE" if args.live else "paper"
    with open(state_dir / "journal.jsonl", "a") as journal:
        for o in plan.orders:
            result = {"status": "planned"} if args.plan_only else broker.place(o)
            print(f"  {o.side} {o.product_id} ${o.usd:.2f} ({o.base_size:.6g} units) "
                  f"stop={o.stop} -> {result['status']}  [{o.reason}]")
            journal.write(json.dumps({"time": dt.datetime.now().isoformat(timespec="seconds"),
                                      "date": args.date, "mode": mode, **o.to_dict(),
                                      "result": result}) + "\n")
    print(f"Plan written to {run_dir / 'orders.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
