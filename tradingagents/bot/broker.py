"""Broker clients for the trading bot: Alpaca (paper/live) and a dry-run stand-in."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import requests

from tradingagents.bot.policy import OrderPlan

logger = logging.getLogger(__name__)

ALPACA_LIVE_URL = "https://api.alpaca.markets"
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

# Order states in which an order no longer reserves shares.
_DONE_STATUSES = {"canceled", "filled", "expired", "rejected", "replaced"}


class BrokerError(RuntimeError):
    """Raised when the broker rejects a request or cannot be reached."""


@dataclass(frozen=True)
class AccountState:
    buying_power: float
    equity: float
    tradable: bool
    status: str


@dataclass(frozen=True)
class Position:
    qty: float
    market_value: float
    price: float


class Broker(Protocol):
    sends_orders: bool

    def account(self) -> AccountState: ...

    def positions(self) -> dict[str, Position]: ...

    def latest_price(self, symbol: str) -> float: ...

    def is_trading_day(self, day: date) -> bool: ...

    def submit(self, plan: OrderPlan, client_order_id: str) -> dict: ...

    def cancel_stops(self, symbol: str) -> int: ...

    def place_stop(self, symbol: str, qty: int, stop_price: float, client_order_id: str) -> dict: ...


class AlpacaBroker:
    """Minimal Alpaca Trading API v2 client (account, positions, calendar, orders)."""

    sends_orders = True
    poll_interval = 0.5
    cancel_timeout = 10.0

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        *,
        paper: bool = True,
        session: requests.Session | None = None,
        timeout: float = 15.0,
    ):
        self.base_url = ALPACA_PAPER_URL if paper else ALPACA_LIVE_URL
        self.paper = paper
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        )

    def _request(self, method: str, path: str, *, base_url: str | None = None, **kwargs):
        url = f"{base_url or self.base_url}{path}"
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise BrokerError(f"Alpaca {method} {path} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise BrokerError(
                f"Alpaca {method} {path} returned {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json() if resp.content else {}

    def account(self) -> AccountState:
        data = self._request("GET", "/v2/account")
        blocked = data.get("trading_blocked") or data.get("account_blocked")
        return AccountState(
            buying_power=float(data.get("buying_power", 0)),
            equity=float(data.get("equity", 0)),
            tradable=data.get("status") == "ACTIVE" and not blocked,
            status=str(data.get("status")),
        )

    def positions(self) -> dict[str, Position]:
        return {
            p["symbol"].upper(): Position(
                qty=float(p["qty"]),
                market_value=float(p["market_value"]),
                price=float(p["current_price"]),
            )
            for p in self._request("GET", "/v2/positions")
        }

    def latest_price(self, symbol: str) -> float:
        data = self._request(
            "GET", f"/v2/stocks/{symbol}/trades/latest",
            base_url=ALPACA_DATA_URL, params={"feed": "iex"},
        )
        try:
            return float(data["trade"]["p"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BrokerError(f"No latest trade for {symbol}: {data}") from exc

    def is_trading_day(self, day: date) -> bool:
        iso = day.isoformat()
        days = self._request("GET", "/v2/calendar", params={"start": iso, "end": iso})
        return any(d.get("date") == iso for d in days)

    def submit(self, plan: OrderPlan, client_order_id: str) -> dict:
        if plan.close_position:
            return self._request("DELETE", f"/v2/positions/{plan.symbol}")
        order = {
            "symbol": plan.symbol,
            "qty": f"{plan.qty:g}",
            "side": plan.side,
            "type": "market",
            "client_order_id": client_order_id,
        }
        if plan.side == "buy":
            # One-triggers-other: the stop leg activates once the entry fills
            # (at the next open when submitted after the close) and stays GTC.
            order.update(
                time_in_force="gtc",
                order_class="oto",
                stop_loss={"stop_price": f"{plan.stop_price:.2f}"},
            )
        else:
            order["time_in_force"] = "day"
        return self._request("POST", "/v2/orders", json=order)

    def cancel_stops(self, symbol: str) -> int:
        """Cancel open stop sells on ``symbol`` so their shares can be sold."""
        orders = self._request(
            "GET", "/v2/orders", params={"status": "open", "symbols": symbol, "nested": "false"}
        )
        stops = [
            o for o in orders
            if o.get("side") == "sell" and o.get("type") in ("stop", "stop_limit")
        ]
        for o in stops:
            self._request("DELETE", f"/v2/orders/{o['id']}")
        # Cancels are asynchronous; the shares stay reserved until each one lands.
        for o in stops:
            self._wait_until_done(o["id"])
        return len(stops)

    def _wait_until_done(self, order_id: str) -> None:
        deadline = time.monotonic() + self.cancel_timeout
        while True:
            status = self._request("GET", f"/v2/orders/{order_id}").get("status")
            if status in _DONE_STATUSES:
                return
            if time.monotonic() >= deadline:
                raise BrokerError(
                    f"Order {order_id} still {status} after {self.cancel_timeout:.0f}s"
                )
            time.sleep(self.poll_interval)

    def place_stop(self, symbol: str, qty: int, stop_price: float, client_order_id: str) -> dict:
        return self._request("POST", "/v2/orders", json={
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "stop",
            "stop_price": f"{stop_price:.2f}",
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        })


def _yfinance_last_close(symbol: str) -> float:
    import yfinance as yf

    closes = yf.Ticker(symbol).history(period="5d")["Close"].dropna()
    if closes.empty:
        raise BrokerError(f"No recent price for {symbol}")
    return float(closes.iloc[-1])


class DryRunBroker:
    """Pretends to be a flat account with buying power equal to the exposure cap.

    Nothing is sent anywhere; orders are only logged. Prices come from
    ``price_lookup`` (Yahoo Finance's last close by default) so share counts
    and stop levels match what a paper or live run would compute.
    """

    sends_orders = False

    def __init__(self, buying_power: float,
                 price_lookup: Callable[[str], float] = _yfinance_last_close):
        self._buying_power = buying_power
        self._price_lookup = price_lookup

    def account(self) -> AccountState:
        return AccountState(self._buying_power, self._buying_power, True, "DRY_RUN")

    def positions(self) -> dict[str, Position]:
        return {}

    def latest_price(self, symbol: str) -> float:
        return self._price_lookup(symbol)

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5

    def submit(self, plan: OrderPlan, client_order_id: str) -> dict:
        logger.info(
            "[dry run] would %s %g %s @ ~$%.2f with stop $%.2f (%s)",
            plan.side, plan.qty, plan.symbol, plan.price, plan.stop_price, client_order_id,
        )
        return {"id": None, "dry_run": True}

    def cancel_stops(self, symbol: str) -> int:
        return 0

    def place_stop(self, symbol: str, qty: int, stop_price: float, client_order_id: str) -> dict:
        logger.info("[dry run] would place stop: sell %d %s @ $%.2f", qty, symbol, stop_price)
        return {"id": None, "dry_run": True}
