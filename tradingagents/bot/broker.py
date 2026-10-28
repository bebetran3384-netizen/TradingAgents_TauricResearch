"""Broker clients for the trading bot: Alpaca (paper/live) and a dry-run stand-in."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import requests

from tradingagents.bot.policy import OrderPlan

logger = logging.getLogger(__name__)

ALPACA_LIVE_URL = "https://api.alpaca.markets"
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"


class BrokerError(RuntimeError):
    """Raised when the broker rejects a request or cannot be reached."""


@dataclass(frozen=True)
class AccountState:
    buying_power: float
    equity: float
    tradable: bool
    status: str


class Broker(Protocol):
    sends_orders: bool

    def account(self) -> AccountState: ...

    def position_values(self) -> dict[str, float]: ...

    def is_trading_day(self, day: date) -> bool: ...

    def submit(self, plan: OrderPlan, client_order_id: str) -> dict: ...


class AlpacaBroker:
    """Minimal Alpaca Trading API v2 client (account, positions, calendar, orders)."""

    sends_orders = True

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

    def _request(self, method: str, path: str, **kwargs):
        try:
            resp = self.session.request(
                method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs
            )
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

    def position_values(self) -> dict[str, float]:
        return {
            p["symbol"].upper(): float(p["market_value"])
            for p in self._request("GET", "/v2/positions")
        }

    def is_trading_day(self, day: date) -> bool:
        iso = day.isoformat()
        days = self._request("GET", "/v2/calendar", params={"start": iso, "end": iso})
        return any(d.get("date") == iso for d in days)

    def submit(self, plan: OrderPlan, client_order_id: str) -> dict:
        if plan.close_position:
            return self._request("DELETE", f"/v2/positions/{plan.symbol}")
        # Notional market orders submitted after the close queue for the next
        # session's open; time_in_force must be "day" for notional orders.
        return self._request(
            "POST",
            "/v2/orders",
            json={
                "symbol": plan.symbol,
                "notional": f"{plan.notional_usd:.2f}",
                "side": plan.side,
                "type": "market",
                "time_in_force": "day",
                "client_order_id": client_order_id,
            },
        )


class DryRunBroker:
    """Pretends to be a flat account with buying power equal to the exposure cap.

    Nothing is sent anywhere; ``submit`` only logs the order that would be placed.
    """

    sends_orders = False

    def __init__(self, buying_power: float):
        self._buying_power = buying_power

    def account(self) -> AccountState:
        return AccountState(self._buying_power, self._buying_power, True, "DRY_RUN")

    def position_values(self) -> dict[str, float]:
        return {}

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5

    def submit(self, plan: OrderPlan, client_order_id: str) -> dict:
        logger.info(
            "[dry run] would %s %s $%.2f (%s)",
            plan.side, plan.symbol, plan.notional_usd, client_order_id,
        )
        return {"id": None, "dry_run": True}
