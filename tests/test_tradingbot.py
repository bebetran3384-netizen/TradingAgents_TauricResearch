import json

import pytest

from tradingbot.brokers import PaperBroker
from tradingbot.planner import Signal, plan_orders
from tradingbot.signals import parse_level, product_id

CFG = {
    "base_allocation_usd": 100,
    "max_total_exposure_usd": 300,
    "max_order_usd": 150,
    "min_order_usd": 5,
    "cash_buffer_usd": 10,
    "max_chase_pct": 3,
    "target_weights": {"Buy": 1.0, "Overweight": 0.5, "Hold": None, "Underweight": 0.25, "Sell": 0.0},
}


def sig(rating, price=10.0, pid="M-USD", entry=None, stop=None, asset="stock"):
    return Signal("M", asset, pid, rating, price, entry, stop)


@pytest.mark.unit
class TestPlanner:
    def test_buy_from_flat_reaches_target(self):
        plan = plan_orders([sig("Buy", stop=9.0)], {}, 1000, CFG)
        (o,) = plan.orders
        assert (o.side, o.usd, o.base_size, o.stop) == ("BUY", 100, 10, 9.0)

    def test_overweight_tops_up_existing_position(self):
        plan = plan_orders([sig("Overweight")], {"M-USD": 3}, 1000, CFG)
        assert plan.orders[0].usd == 20

    def test_cash_buffer_caps_buy(self):
        plan = plan_orders([sig("Buy")], {}, 50, CFG)
        assert plan.orders[0].usd == 40

    def test_no_cash_skips_with_reason(self):
        plan = plan_orders([sig("Buy")], {}, 0.003, CFG)
        assert not plan.orders and "cash" in plan.skipped[0].reason

    def test_total_exposure_limit_spans_symbols(self):
        cfg = {**CFG, "max_total_exposure_usd": 150}
        plan = plan_orders([sig("Buy", pid="A-USD"), sig("Buy", pid="B-USD")], {}, 1000, cfg)
        assert [o.usd for o in plan.orders] == [100, 50]

    def test_does_not_chase_above_entry(self):
        plan = plan_orders([sig("Buy", price=24.72, entry=23.85)], {}, 1000, CFG)
        assert not plan.orders and "not chasing" in plan.skipped[0].reason

    def test_buys_within_chase_tolerance(self):
        assert plan_orders([sig("Buy", price=24.0, entry=23.85)], {}, 1000, CFG).orders

    def test_stop_above_price_is_dropped(self):
        assert plan_orders([sig("Buy", stop=11.0)], {}, 1000, CFG).orders[0].stop is None

    def test_sell_exits_whole_position(self):
        (o,) = plan_orders([sig("Sell")], {"M-USD": 4}, 0, CFG).orders
        assert (o.side, o.base_size) == ("SELL", 4)

    def test_underweight_trims(self):
        (o,) = plan_orders([sig("Underweight")], {"M-USD": 10}, 0, CFG).orders
        assert (o.side, o.base_size) == ("SELL", 7.5)

    def test_underweight_never_opens_a_position(self):
        plan = plan_orders([sig("Underweight")], {}, 1000, CFG)
        assert not plan.orders and "never buys" in plan.skipped[0].reason

    def test_underweight_below_target_does_not_top_up(self):
        assert not plan_orders([sig("Underweight")], {"M-USD": 1}, 1000, CFG).orders

    def test_sell_with_nothing_held_does_nothing(self):
        assert not plan_orders([sig("Sell")], {}, 1000, CFG).orders

    @pytest.mark.parametrize("rating", ["Hold", "REVIEW"])
    def test_hold_and_review_never_trade(self, rating):
        assert not plan_orders([sig(rating)], {"M-USD": 1}, 1000, CFG).orders


@pytest.mark.unit
def test_parse_levels_from_trader_proposal():
    text = "**Entry Price**: 23.85\n\n**Stop Loss**: $1,022.5\n\n**Position Sizing**: not provided"
    assert parse_level(text, "Entry Price") == 23.85
    assert parse_level(text, "Stop Loss") == 1022.5
    assert parse_level("**Entry Price**: not provided", "Entry Price") is None


@pytest.mark.unit
def test_product_ids():
    assert product_id("m", "stock") == "M-USD"
    assert product_id("ETH", "crypto") == "ETH-USD"
    assert product_id("BTC-USD", "crypto") == "BTC-USD"


@pytest.mark.unit
def test_paper_broker_round_trip(tmp_path):
    from tradingbot.planner import Order

    book = tmp_path / "paper.json"
    b = PaperBroker(book, starting_cash=100)
    b.place(Order("M-USD", "stock", "BUY", 50, 5, 10))
    b.place(Order("M-USD", "stock", "SELL", 60, 5, 12))
    saved = json.loads(book.read_text())
    assert saved == {"cash": 110, "positions": {}}


@pytest.mark.unit
def test_robinhood_queue_broker(tmp_path):
    from tradingbot.brokers import RobinhoodQueueBroker
    from tradingbot.planner import Order

    snap = tmp_path / "robinhood_account.json"
    snap.write_text(json.dumps({"cash": 659.91, "positions": {"BTC-USD": 0.001}}))
    queue = tmp_path / "queue.json"
    b = RobinhoodQueueBroker(snap, queue)
    assert b.cash() == 659.91 and b.positions() == {"BTC-USD": 0.001}

    b.place(Order("ETH-USD", "crypto", "BUY", 100, 0.04, 2500, stop=2300))
    b.place(Order("M-USD", "stock", "SELL", 50, 2, 25))
    eth, m = json.loads(queue.read_text())
    assert (eth["symbol"], eth["stop"], eth["status"]) == ("ETH-USD", 2300, "queued")
    assert m["symbol"] == "M" and eth["ref_id"] != m["ref_id"]


@pytest.mark.unit
def test_robinhood_queue_broker_needs_snapshot(tmp_path):
    from tradingbot.brokers import RobinhoodQueueBroker

    with pytest.raises(RuntimeError, match="snapshot"):
        RobinhoodQueueBroker(tmp_path / "missing.json", tmp_path / "queue.json")
