"""Where orders go: a local paper account, or Coinbase with real money.

The Coinbase Advanced Trade SDK trades spot crypto only, so in live mode stock
orders are not sent: they are returned as "manual" for you to place yourself
(or through Claude's Coinbase connector, which asks you to approve each one).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from tradingbot.planner import Order


class PaperBroker:
    """Fills market orders at the planning price against a local JSON ledger."""

    def __init__(self, path: Path, starting_cash: float = 1000.0):
        self.path = path
        if path.exists():
            self.book = json.loads(path.read_text())
        else:
            self.book = {"cash": starting_cash, "positions": {}}

    def cash(self) -> float:
        return float(self.book["cash"])

    def positions(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.book["positions"].items()}

    def place(self, order: Order) -> dict:
        pos = self.book["positions"]
        if order.side == "BUY":
            self.book["cash"] -= order.usd
            pos[order.product_id] = pos.get(order.product_id, 0.0) + order.base_size
        else:
            self.book["cash"] += order.usd
            pos[order.product_id] = pos.get(order.product_id, 0.0) - order.base_size
            if pos[order.product_id] <= 1e-12:
                del pos[order.product_id]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.book, indent=2))
        return {"status": "filled (paper)", "stop": order.stop}


class CoinbaseBroker:
    """Real orders through the Coinbase Advanced Trade API (crypto spot only).

    Needs COINBASE_API_KEY and COINBASE_API_SECRET (a CDP API key with trade
    permission). A BUY is a market order; if the agents gave a stop, a GTC
    stop-limit sell for the filled size is placed right after.
    """

    STOP_LIMIT_GAP = 0.01  # the stop's limit sits 1% under the trigger so it can fill

    def __init__(self, portfolio_id: str | None = None):
        from coinbase.rest import RESTClient

        if not (os.getenv("COINBASE_API_KEY") and os.getenv("COINBASE_API_SECRET")):
            raise RuntimeError("Live mode needs COINBASE_API_KEY and COINBASE_API_SECRET.")
        self.client = RESTClient()
        self.portfolio_id = portfolio_id
        self._accounts = None

    def _balances(self) -> dict[str, float]:
        if self._accounts is None:
            self._accounts, cursor = {}, None
            while True:
                page = self.client.get_accounts(limit=250, cursor=cursor).to_dict()
                for a in page["accounts"]:
                    self._accounts[a["currency"]] = float(a["available_balance"]["value"])
                if not page.get("has_next"):
                    break
                cursor = page["cursor"]
        return self._accounts

    def cash(self) -> float:
        return self._balances().get("USD", 0.0)

    def positions(self) -> dict[str, float]:
        # Crypto only: stock holdings are not visible through this API.
        return {f"{c}-USD": q for c, q in self._balances().items() if c not in ("USD", "USDC") and q > 0}

    def place(self, order: Order) -> dict:
        if order.asset_type != "crypto":
            return {"status": "manual", "note": "stock orders are not supported by the Coinbase SDK"}
        oid = str(uuid.uuid4())
        extra = {"retail_portfolio_id": self.portfolio_id} if self.portfolio_id else {}
        if order.side == "BUY":
            res = self.client.market_order_buy(oid, order.product_id,
                                               quote_size=f"{order.usd:.2f}", **extra).to_dict()
        else:
            res = self.client.market_order_sell(oid, order.product_id,
                                                base_size=self._size(order.product_id, order.base_size),
                                                **extra).to_dict()
        if not res.get("success"):
            return {"status": "rejected", "response": res}
        out = {"status": "submitted", "order_id": res["success_response"]["order_id"]}
        if order.side == "BUY" and order.stop:
            out["stop"] = self._place_stop(order, out["order_id"])
        return out

    def _place_stop(self, order: Order, order_id: str) -> dict:
        filled = 0.0
        for _ in range(10):  # a market order normally fills within a second or two
            o = self.client.get_order(order_id).to_dict()["order"]
            filled = float(o.get("filled_size") or 0)
            if o.get("status") in ("FILLED", "CANCELLED", "EXPIRED", "FAILED"):
                break
            time.sleep(0.5)
        if filled <= 0:
            return {"status": "not placed", "note": "buy not filled yet; set the stop by hand"}
        res = self.client.stop_limit_order_gtc_sell(
            str(uuid.uuid4()), order.product_id, base_size=self._size(order.product_id, filled),
            limit_price=self._step(order.product_id, order.stop * (1 - self.STOP_LIMIT_GAP), "quote_increment"),
            stop_price=self._step(order.product_id, order.stop, "quote_increment"),
            stop_direction="STOP_DIRECTION_STOP_DOWN",
        ).to_dict()
        return {"status": "placed" if res.get("success") else "rejected", "response": res}


    def _size(self, product_id: str, units: float) -> str:
        return self._step(product_id, units, "base_increment")

    def _step(self, product_id: str, value: float, field: str) -> str:
        """Round down to the product's size or price increment, as Coinbase requires."""
        step = Decimal(self.client.get_product(product_id).to_dict()[field])
        return str((Decimal(str(value)) / step).to_integral_value(ROUND_DOWN) * step)
