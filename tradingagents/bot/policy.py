"""Map a 5-tier rating to a position change, subject to the bot's risk caps."""

from __future__ import annotations

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
    notional_usd: float
    close_position: bool = False
    reason: str = ""


def plan_order(
    symbol: str,
    rating: str,
    current_value: float,
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
    if current_value < 0:
        return None, "short position held; bot only manages long positions"

    target = weight * max_position_usd
    delta = target - current_value

    if rating in _BULLISH:
        if delta <= 0:
            return None, f"{rating}: already at or above target ${target:,.2f}"
        capped = min(delta, exposure_headroom_usd, buying_power_usd)
        if capped < min_trade_usd:
            return None, (
                f"{rating}: buy ${delta:,.2f} wanted, ${capped:,.2f} allowed by caps "
                f"(< ${min_trade_usd:,.2f} minimum)"
            )
        note = "" if capped == delta else f" (capped from ${delta:,.2f})"
        return OrderPlan(symbol, "buy", round(capped, 2), reason=f"{rating}{note}"), (
            f"{rating}: buy ${capped:,.2f} toward target ${target:,.2f}{note}"
        )

    if rating in _BEARISH:
        if current_value <= 0:
            return None, f"{rating}: no position to reduce"
        if target == 0:
            return OrderPlan(symbol, "sell", round(current_value, 2), True, rating), (
                f"{rating}: close position (${current_value:,.2f})"
            )
        if -delta < min_trade_usd:
            return None, f"{rating}: already at or below target ${target:,.2f}"
        return OrderPlan(symbol, "sell", round(-delta, 2), reason=rating), (
            f"{rating}: sell ${-delta:,.2f} down to target ${target:,.2f}"
        )

    return None, f"unrecognised rating {rating!r}: no change"
