"""Map a 5-tier rating to a position change, subject to the bot's risk caps.

Orders are sized in whole shares: Alpaca only keeps a protective stop working
across sessions (GTC) for whole-share quantities, and every bot buy carries one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Fraction of max_position_usd to hold for each rating. ``None`` means "leave
# the position as it is".
TARGET_WEIGHTS: dict[str, float | None] = {
    "Buy": 1.0,
    "Overweight": 0.75,
    "Hold": None,
    "Underweight": 0.25,
    "Sell": 0.0,
}

_BULLISH = {"Buy", "Overweight"}
_BEARISH = {"Underweight", "Sell"}


@dataclass(frozen=True)
class OrderPlan:
    symbol: str
    side: str  # "buy" or "sell"
    qty: float
    price: float
    stop_price: float
    close_position: bool = False
    reason: str = ""

    @property
    def notional_usd(self) -> float:
        return round(self.qty * self.price, 2)


def protective_stop(price: float, suggested: float | None, max_loss_pct: float) -> float:
    """Stop price for a long position bought at ``price``.

    The Portfolio Manager's ``suggested`` stop is used when it sits below the
    price, but never further away than ``max_loss_pct``: the PM can tighten
    the bot's stop, not loosen it.
    """
    floor = price * (1 - max_loss_pct)
    stop = suggested if suggested is not None and floor < suggested < price else floor
    return math.floor(stop * 100) / 100


def plan_order(
    symbol: str,
    rating: str,
    current_qty: float,
    price: float,
    stop_price: float,
    *,
    max_position_usd: float,
    min_trade_usd: float,
    exposure_headroom_usd: float,
    buying_power_usd: float,
) -> tuple[OrderPlan | None, str]:
    """Return ``(plan, explanation)``; ``plan`` is ``None`` when no order is due.

    Bullish ratings only ever buy and bearish ratings only ever sell, so a
    position that drifted above its target on a Buy is not trimmed.
    """
    weight = TARGET_WEIGHTS.get(rating)
    if weight is None:
        return None, f"{rating}: no change"
    if current_qty < 0:
        return None, "short position held; bot only manages long positions"

    current_value = current_qty * price
    target = weight * max_position_usd
    delta = target - current_value

    if rating in _BULLISH:
        if delta <= 0:
            return None, f"{rating}: already at or above target ${target:,.2f}"
        budget = min(delta, exposure_headroom_usd, buying_power_usd)
        qty = math.floor(budget / price)
        if qty < 1 or qty * price < min_trade_usd:
            return None, (
                f"{rating}: ${budget:,.2f} allowed by caps buys {qty} share(s) at "
                f"${price:,.2f} (< ${min_trade_usd:,.2f} minimum)"
            )
        plan = OrderPlan(symbol, "buy", qty, price, stop_price, reason=rating)
        return plan, (
            f"{rating}: buy {qty} @ ~${price:,.2f} (${plan.notional_usd:,.2f}) toward target "
            f"${target:,.2f}, stop ${stop_price:,.2f}"
        )

    if rating in _BEARISH:
        if current_qty <= 0:
            return None, f"{rating}: no position to reduce"
        if target == 0:
            plan = OrderPlan(symbol, "sell", current_qty, price, stop_price, True, rating)
            return plan, f"{rating}: close position ({current_qty:g} shares)"
        qty = math.floor(-delta / price)
        if qty < 1 or qty * price < min_trade_usd:
            return None, f"{rating}: already at or near target ${target:,.2f}"
        plan = OrderPlan(symbol, "sell", qty, price, stop_price, reason=rating)
        return plan, (
            f"{rating}: sell {qty} @ ~${price:,.2f} down to target ${target:,.2f}, "
            f"stop ${stop_price:,.2f} on the rest"
        )

    return None, f"unrecognised rating {rating!r}: no change"
