"""Turn TradingAgents ratings into orders, inside hard dollar limits.

Pure functions only: no network, no broker, so every rule here is unit-tested.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Ratings that may only shrink a position: Underweight trims toward its target
# but must not open one from nothing.
BEARISH = frozenset({"Underweight", "Sell"})


@dataclass
class Signal:
    symbol: str                 # what TradingAgents analyzed, e.g. "M" or "ETH-USD"
    asset_type: str             # "stock" or "crypto"
    product_id: str             # Coinbase product, e.g. "M-USD" or "ETH-USD"
    rating: str                 # Buy / Overweight / Hold / Underweight / Sell / REVIEW
    price: float                # latest price the plan is sized against
    entry: float | None = None  # the trader agent's entry level, if it gave one
    stop: float | None = None   # the trader agent's stop level, if it gave one


@dataclass
class Order:
    product_id: str
    asset_type: str
    side: str                   # BUY or SELL
    usd: float                  # notional at the planning price
    base_size: float            # units (shares or coins)
    price: float
    stop: float | None = None   # stop to place after a BUY fills
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Skip:
    product_id: str
    reason: str


@dataclass
class Plan:
    orders: list[Order] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)


def plan_orders(signals: list[Signal], positions: dict[str, float], cash: float,
                cfg: dict) -> Plan:
    """Plan orders that move each holding toward its rating's target weight.

    ``positions`` maps product_id to units held. Buys are capped, in order, by
    the per-order limit, the room left under the total-exposure limit, and the
    cash above the buffer. Sale proceeds are not counted as cash for buys in the
    same run, so a plan never depends on a sell filling first.
    """
    weights = cfg["target_weights"]
    base = float(cfg["base_allocation_usd"])
    max_order = float(cfg["max_order_usd"])
    min_order = float(cfg["min_order_usd"])
    max_chase = float(cfg.get("max_chase_pct", 3.0)) / 100

    prices = {s.product_id: s.price for s in signals}
    # Only watchlist holdings count toward the limit: the bot sizes what it trades.
    exposure = sum(qty * prices[p] for p, qty in positions.items() if p in prices)
    spendable = max(0.0, cash - float(cfg["cash_buffer_usd"]))
    room = max(0.0, float(cfg["max_total_exposure_usd"]) - exposure)

    plan = Plan()
    for s in signals:
        if s.rating not in weights:
            plan.skipped.append(Skip(s.product_id, f"rating {s.rating!r} is not tradeable"))
            continue
        weight = weights[s.rating]
        if weight is None:
            plan.skipped.append(Skip(s.product_id, f"{s.rating}: keep the current position"))
            continue
        if not s.price or s.price <= 0:
            plan.skipped.append(Skip(s.product_id, "no price"))
            continue

        held = positions.get(s.product_id, 0.0)
        diff = weight * base - held * s.price

        if diff > 0:
            if s.rating in BEARISH:
                plan.skipped.append(Skip(s.product_id, f"{s.rating}: a bearish rating never buys"))
                continue
            if s.entry and s.price > s.entry * (1 + max_chase):
                plan.skipped.append(Skip(
                    s.product_id,
                    f"{s.rating}, but price {s.price:.2f} is more than {max_chase:.0%} "
                    f"above the agents' entry {s.entry:.2f}; not chasing"))
                continue
            caps = {"target": diff, "per-order limit": max_order,
                    "total-exposure limit": room, "cash": spendable}
            binding = min(caps, key=caps.get)
            usd = caps[binding]
            if usd < min_order:
                plan.skipped.append(Skip(
                    s.product_id, f"{s.rating}: ${usd:.2f} allowed by {binding}, "
                                  f"under the ${min_order:.2f} minimum order"))
                continue
            stop = s.stop if s.stop and s.stop < s.price else None
            plan.orders.append(Order(s.product_id, s.asset_type, "BUY", round(usd, 2),
                                     usd / s.price, s.price, stop,
                                     f"{s.rating}: target ${weight * base:.2f}"))
            room -= usd
            spendable -= usd
        elif -diff >= min_order and held > 0:
            units = min(held, -diff / s.price)
            plan.orders.append(Order(s.product_id, s.asset_type, "SELL",
                                     round(units * s.price, 2), units, s.price, None,
                                     f"{s.rating}: target ${weight * base:.2f}"))
        else:
            plan.skipped.append(Skip(s.product_id, f"{s.rating}: already at target"))
    return plan
