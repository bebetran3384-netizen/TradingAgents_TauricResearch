"""Tests for the scheduled trading bot (config, sizing policy, runner, scheduler, Alpaca client)."""

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
import pytz

from tradingagents.bot.__main__ import next_run_at
from tradingagents.bot.broker import (
    AccountState,
    AlpacaBroker,
    BrokerError,
    DryRunBroker,
    Position,
)
from tradingagents.bot.config import BotConfig, BotConfigError
from tradingagents.bot.policy import OrderPlan, plan_order, protective_stop
from tradingagents.bot.runner import Analysis, Journal, parse_stop_loss, run_once

TRADE_DATE = date(2026, 10, 27)  # a Tuesday


def _plan(rating, qty=0.0, price=100.0, *, headroom=10_000.0, buying_power=10_000.0):
    return plan_order(
        "NVDA", rating, qty, price, 90.0,
        max_position_usd=1000.0, min_trade_usd=50.0,
        exposure_headroom_usd=headroom, buying_power_usd=buying_power,
    )


class FakeBroker:
    sends_orders = True

    def __init__(self, positions=None, prices=None, buying_power=100_000.0, trading_day=True,
                 tradable=True, fail_on=(), fail_stops=False):
        self._positions = positions or {}
        self.prices = prices or {}
        self.buying_power = buying_power
        self.trading_day = trading_day
        self.tradable = tradable
        self.fail_on = set(fail_on)
        self.fail_stops = fail_stops
        self.submitted = []
        self.cancelled = []
        self.stops = []

    def account(self):
        return AccountState(self.buying_power, self.buying_power, self.tradable, "ACTIVE")

    def positions(self):
        return dict(self._positions)

    def latest_price(self, symbol):
        if symbol not in self.prices:
            raise BrokerError(f"no price for {symbol}")
        return self.prices[symbol]

    def is_trading_day(self, day):
        return self.trading_day

    def submit(self, plan, client_order_id):
        if plan.symbol in self.fail_on:
            raise BrokerError("rejected")
        self.submitted.append((plan, client_order_id))
        return {"id": f"order-{plan.symbol}"}

    def cancel_stops(self, symbol):
        self.cancelled.append(symbol)
        return 1

    def place_stop(self, symbol, qty, stop_price, client_order_id):
        if self.fail_stops:
            raise BrokerError("stop rejected")
        self.stops.append((symbol, qty, stop_price))
        return {"id": f"stop-{symbol}"}


@pytest.fixture
def cfg(tmp_path):
    return BotConfig(
        watchlist=("NVDA", "AAPL", "MSFT"),
        mode="paper",
        max_position_usd=1000.0,
        max_total_exposure_usd=1500.0,
        min_trade_usd=50.0,
        stop_loss_pct=0.10,
        state_dir=tmp_path,
        alpaca_key_id="k",
        alpaca_secret_key="s",
    )


PRICES = {"NVDA": 100.0, "AAPL": 50.0, "MSFT": 400.0}


def _ratings(**by_ticker):
    calls = []

    def analyze(ticker, trade_date):
        calls.append((ticker, trade_date))
        result = by_ticker[ticker]
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, Analysis) else Analysis(result)

    analyze.calls = calls
    return analyze


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBotConfig:
    def test_defaults_are_dry_run(self):
        cfg = BotConfig.from_env({"TRADING_BOT_WATCHLIST": " nvda, aapl ,NVDA,"})
        assert cfg.mode == "dry_run"
        assert cfg.watchlist == ("NVDA", "AAPL")
        assert cfg.stop_loss_pct == 0.10

    def test_stop_loss_pct_is_percent(self):
        cfg = BotConfig.from_env({"TRADING_BOT_WATCHLIST": "NVDA", "TRADING_BOT_STOP_LOSS_PCT": "7.5"})
        assert cfg.stop_loss_pct == pytest.approx(0.075)

    def test_empty_watchlist_rejected(self):
        with pytest.raises(BotConfigError, match="WATCHLIST"):
            BotConfig.from_env({})

    def test_paper_requires_alpaca_keys(self):
        with pytest.raises(BotConfigError, match="ALPACA"):
            BotConfig.from_env({"TRADING_BOT_WATCHLIST": "NVDA", "TRADING_BOT_MODE": "paper"})

    def test_live_requires_explicit_confirmation(self):
        env = {
            "TRADING_BOT_WATCHLIST": "NVDA", "TRADING_BOT_MODE": "live",
            "ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s",
        }
        with pytest.raises(BotConfigError, match="CONFIRM_LIVE"):
            BotConfig.from_env(env)
        assert BotConfig.from_env({**env, "TRADING_BOT_CONFIRM_LIVE": "yes"}).mode == "live"

    @pytest.mark.parametrize("key,value", [
        ("TRADING_BOT_MODE", "yolo"),
        ("TRADING_BOT_MAX_POSITION_USD", "-5"),
        ("TRADING_BOT_MAX_POSITION_USD", "lots"),
        ("TRADING_BOT_MAX_TOTAL_EXPOSURE_USD", "10"),
        ("TRADING_BOT_MIN_TRADE_USD", "0"),
        ("TRADING_BOT_STOP_LOSS_PCT", "0"),
        ("TRADING_BOT_STOP_LOSS_PCT", "60"),
        ("TRADING_BOT_RUN_TIME", "25:00"),
        ("TRADING_BOT_TIMEZONE", "Mars/Olympus"),
    ])
    def test_invalid_values_rejected(self, key, value):
        with pytest.raises(BotConfigError):
            BotConfig.from_env({"TRADING_BOT_WATCHLIST": "NVDA", key: value})


# ---------------------------------------------------------------------------
# Stop price
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProtectiveStop:
    def test_uses_pm_stop_inside_the_limit(self):
        assert protective_stop(217.94, 199.5, 0.10) == 199.5

    def test_pm_stop_cannot_be_looser_than_limit(self):
        assert protective_stop(100.0, 70.0, 0.10) == 90.0

    @pytest.mark.parametrize("suggested", [None, 100.0, 120.0])
    def test_missing_or_invalid_pm_stop_falls_back_to_limit(self, suggested):
        assert protective_stop(100.0, suggested, 0.10) == 90.0

    def test_rounds_down_to_cents(self):
        assert protective_stop(217.94, None, 0.10) == 196.14

    @pytest.mark.parametrize("text,expected", [
        ("**Rating**: Buy\n\n**Stop Loss**: 199.5\n", 199.5),
        ("**Stop Loss**: $1,203.25", 1203.25),
        ("**Rating**: Hold\n\nMaintain a hard stop-loss at $199.5.", None),
        ("", None),
    ])
    def test_parse_stop_loss(self, text, expected):
        assert parse_stop_loss(text) == expected


# ---------------------------------------------------------------------------
# Sizing policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPlanOrder:
    def test_buy_from_flat_targets_full_position_in_whole_shares(self):
        plan, _ = _plan("Buy", price=218.0)
        assert plan == OrderPlan("NVDA", "buy", 4, 218.0, 90.0, reason="Buy")
        assert plan.notional_usd == 872.0

    def test_overweight_targets_three_quarters(self):
        plan, _ = _plan("Overweight", qty=5)
        assert (plan.side, plan.qty) == ("buy", 2)

    def test_hold_never_trades(self):
        assert _plan("Hold", qty=3)[0] is None

    def test_buy_above_target_does_not_trim(self):
        assert _plan("Buy", qty=12)[0] is None

    def test_underweight_trims_to_quarter(self):
        plan, _ = _plan("Underweight", qty=10)
        assert (plan.side, plan.qty, plan.close_position) == ("sell", 7, False)

    def test_underweight_below_target_does_not_buy(self):
        assert _plan("Underweight", qty=1)[0] is None

    def test_sell_closes_whole_position(self):
        plan, _ = _plan("Sell", qty=6.4)
        assert plan.close_position and (plan.side, plan.qty) == ("sell", 6.4)

    def test_sell_with_no_position_is_noop(self):
        assert _plan("Sell")[0] is None

    def test_buy_capped_by_exposure_and_buying_power(self):
        assert _plan("Buy", headroom=350.0)[0].qty == 3
        assert _plan("Buy", buying_power=120.0)[0].qty == 1

    def test_buy_skipped_when_caps_allow_no_whole_share(self):
        assert _plan("Buy", price=1200.0)[0] is None

    def test_buy_below_minimum_after_caps_is_skipped(self):
        assert _plan("Buy", price=40.0, headroom=45.0)[0] is None

    def test_short_position_left_alone(self):
        assert _plan("Sell", qty=-5)[0] is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunOnce:
    def test_trades_each_ticker_and_respects_total_exposure(self, cfg):
        broker = FakeBroker(prices=PRICES)
        analyze = _ratings(NVDA=Analysis("Buy", stop_loss=95.0), AAPL="Buy", MSFT="Hold")
        entries = run_once(cfg, TRADE_DATE, broker=broker, analyze=analyze)

        # 1500 total cap: NVDA gets 10 x $100, AAPL the remaining $500 = 10 x $50.
        assert [(p.symbol, p.qty, p.stop_price) for p, _ in broker.submitted] == [
            ("NVDA", 10, 95.0), ("AAPL", 10, 45.0),
        ]
        assert broker.submitted[0][1] == "ta-2026-10-27-NVDA"
        assert [e["status"] for e in entries] == ["submitted", "submitted", "no_trade"]
        assert entries[0]["pm_stop_loss"] == 95.0 and entries[0]["stop_price"] == 95.0
        assert analyze.calls[0] == ("NVDA", "2026-10-27")
        assert broker.cancelled == [] and broker.stops == []

    def test_trim_cancels_stops_then_reprotects_the_rest(self, cfg):
        broker = FakeBroker(positions={"NVDA": Position(10, 1000.0, 100.0)})
        run_once(cfg, TRADE_DATE, broker=broker,
                 analyze=_ratings(NVDA=Analysis("Underweight", stop_loss=92.0),
                                  AAPL="Hold", MSFT="Hold"))
        assert broker.cancelled == ["NVDA"]
        assert [(p.side, p.qty) for p, _ in broker.submitted] == [("sell", 7)]
        assert broker.stops == [("NVDA", 3, 92.0)]

    def test_close_cancels_stops_without_replacing(self, cfg):
        broker = FakeBroker(positions={"NVDA": Position(10, 1000.0, 100.0)})
        run_once(cfg, TRADE_DATE, broker=broker,
                 analyze=_ratings(NVDA="Sell", AAPL="Hold", MSFT="Hold"))
        assert broker.cancelled == ["NVDA"]
        assert broker.submitted[0][0].close_position
        assert broker.stops == []

    def test_failed_sell_restores_stop_on_whole_position(self, cfg):
        broker = FakeBroker(positions={"NVDA": Position(10, 1000.0, 100.0)}, fail_on={"NVDA"})
        entries = run_once(cfg, TRADE_DATE, broker=broker,
                           analyze=_ratings(NVDA="Underweight", AAPL="Hold", MSFT="Hold"))
        assert entries[0]["status"] == "error"
        assert broker.stops == [("NVDA", 10, 90.0)]

    def test_stop_failure_is_journaled(self, cfg):
        broker = FakeBroker(positions={"NVDA": Position(10, 1000.0, 100.0)}, fail_stops=True)
        entries = run_once(cfg, TRADE_DATE, broker=broker,
                           analyze=_ratings(NVDA="Underweight", AAPL="Hold", MSFT="Hold"))
        assert entries[0]["status"] == "submitted"
        assert entries[0]["stop_error"] == "stop rejected"

    def test_rerun_same_day_only_retries_failures(self, cfg):
        analyze = _ratings(NVDA="Buy", AAPL=RuntimeError("LLM down"), MSFT="Sell")
        first = run_once(cfg, TRADE_DATE, broker=FakeBroker(prices=PRICES), analyze=analyze)
        assert [e["status"] for e in first] == ["submitted", "error", "no_trade"]

        broker = FakeBroker(prices=PRICES)
        analyze = _ratings(AAPL="Buy")
        run_once(cfg, TRADE_DATE, broker=broker, analyze=analyze)
        assert analyze.calls == [("AAPL", "2026-10-27")]
        assert [p.symbol for p, _ in broker.submitted] == ["AAPL"]

    def test_missing_price_is_journaled_and_run_continues(self, cfg):
        broker = FakeBroker(prices={"AAPL": 50.0, "MSFT": 400.0})
        entries = run_once(cfg, TRADE_DATE, broker=broker,
                           analyze=_ratings(NVDA="Buy", AAPL="Buy", MSFT="Hold"))
        assert entries[0]["status"] == "error" and "price" in entries[0]["detail"]
        assert [p.symbol for p, _ in broker.submitted] == ["AAPL"]

    def test_broker_rejection_is_journaled_and_run_continues(self, cfg):
        broker = FakeBroker(prices=PRICES, fail_on={"NVDA"})
        entries = run_once(cfg, TRADE_DATE, broker=broker,
                           analyze=_ratings(NVDA="Buy", AAPL="Buy", MSFT="Hold"))
        assert entries[0]["status"] == "error" and "rejected" in entries[0]["detail"]
        # The rejected buy did not consume exposure headroom.
        assert [(p.symbol, p.qty) for p, _ in broker.submitted] == [("AAPL", 20)]

    def test_kill_switch_blocks_everything(self, cfg):
        cfg.kill_switch_path.touch()
        broker = FakeBroker()
        analyze = _ratings()
        assert run_once(cfg, TRADE_DATE, broker=broker, analyze=analyze) == []
        assert analyze.calls == [] and broker.submitted == []

    def test_non_trading_day_skips_analysis(self, cfg):
        analyze = _ratings()
        assert run_once(cfg, TRADE_DATE, broker=FakeBroker(trading_day=False),
                        analyze=analyze) == []
        assert analyze.calls == []

    def test_untradable_account_aborts(self, cfg):
        with pytest.raises(BrokerError, match="not tradable"):
            run_once(cfg, TRADE_DATE, broker=FakeBroker(tradable=False), analyze=_ratings())

    def test_existing_positions_count_toward_exposure(self, cfg):
        broker = FakeBroker(positions={"MSFT": Position(3, 1200.0, 400.0)}, prices=PRICES)
        run_once(cfg, TRADE_DATE, broker=broker,
                 analyze=_ratings(NVDA="Buy", AAPL="Buy", MSFT="Hold"))
        assert [(p.symbol, p.qty) for p, _ in broker.submitted] == [("NVDA", 3)]

    def test_dry_run_records_without_sending(self, cfg):
        broker = DryRunBroker(buying_power=5000.0, price_lookup=PRICES.__getitem__)
        entries = run_once(cfg, TRADE_DATE, broker=broker,
                           analyze=_ratings(NVDA="Buy", AAPL="Sell", MSFT="Hold"))
        assert [e["status"] for e in entries] == ["dry_run", "no_trade", "no_trade"]
        assert (entries[0]["qty"], entries[0]["stop_price"]) == (10, 90.0)

    def test_journal_ignores_corrupt_lines(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        path.write_text('not json\n{"date": "2026-10-27", "ticker": "NVDA", "status": "submitted"}\n')
        assert Journal(path).done("2026-10-27") == {"NVDA"}


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNextRunAt:
    tz = pytz.timezone("America/New_York")

    def _at(self, *args):
        return self.tz.localize(datetime(*args))

    def test_later_today(self):
        assert next_run_at(self._at(2026, 10, 27, 9, 0), 16, 30) == self._at(2026, 10, 27, 16, 30)

    def test_after_run_time_rolls_to_tomorrow(self):
        assert next_run_at(self._at(2026, 10, 27, 16, 30), 16, 30) == self._at(2026, 10, 28, 16, 30)

    def test_friday_evening_skips_weekend(self):
        assert next_run_at(self._at(2026, 10, 30, 18, 0), 16, 30) == self._at(2026, 11, 2, 16, 30)

    def test_keeps_wall_clock_across_dst_change(self):
        # US DST ends 2026-11-01; Friday 10-30 evening -> Monday 11-02 at 16:30 EST.
        nxt = next_run_at(self._at(2026, 10, 30, 18, 0), 16, 30)
        assert (nxt.hour, nxt.minute, nxt.utcoffset().total_seconds()) == (16, 30, -5 * 3600)


# ---------------------------------------------------------------------------
# Alpaca client
# ---------------------------------------------------------------------------


def _response(status=200, payload=None):
    resp = MagicMock(status_code=status, content=b"x", text=str(payload))
    resp.json.return_value = payload
    return resp


@pytest.mark.unit
class TestAlpacaBroker:
    def _broker(self, *responses, paper=True):
        session = MagicMock(headers={})
        session.request.side_effect = list(responses)
        return AlpacaBroker("key", "secret", paper=paper, session=session), session

    def test_paper_and_live_endpoints(self):
        assert self._broker()[0].base_url == "https://paper-api.alpaca.markets"
        assert self._broker(paper=False)[0].base_url == "https://api.alpaca.markets"

    def test_buy_is_oto_with_gtc_stop(self):
        broker, session = self._broker(_response(payload={"id": "abc"}))
        broker.submit(OrderPlan("NVDA", "buy", 3, 217.94, 199.5), "ta-2026-10-27-NVDA")
        method, url = session.request.call_args.args
        assert (method, url) == ("POST", "https://paper-api.alpaca.markets/v2/orders")
        assert session.request.call_args.kwargs["json"] == {
            "symbol": "NVDA", "qty": "3", "side": "buy", "type": "market",
            "time_in_force": "gtc", "order_class": "oto",
            "stop_loss": {"stop_price": "199.50"},
            "client_order_id": "ta-2026-10-27-NVDA",
        }

    def test_trim_is_plain_day_sell(self):
        broker, session = self._broker(_response(payload={"id": "abc"}))
        broker.submit(OrderPlan("NVDA", "sell", 7, 100.0, 90.0), "id")
        assert session.request.call_args.kwargs["json"] == {
            "symbol": "NVDA", "qty": "7", "side": "sell", "type": "market",
            "time_in_force": "day", "client_order_id": "id",
        }

    def test_close_uses_position_endpoint(self):
        broker, session = self._broker(_response(payload={"id": "x"}))
        broker.submit(OrderPlan("NVDA", "sell", 6.4, 100.0, 90.0, close_position=True), "id")
        assert session.request.call_args.args == (
            "DELETE", "https://paper-api.alpaca.markets/v2/positions/NVDA",
        )

    def test_cancel_stops_only_cancels_stop_sells(self):
        broker, session = self._broker(
            _response(payload=[
                {"id": "s1", "side": "sell", "type": "stop"},
                {"id": "b1", "side": "buy", "type": "market"},
                {"id": "l1", "side": "sell", "type": "limit"},
            ]),
            _response(payload=None),
            _response(payload={"status": "pending_cancel"}),
            _response(payload={"status": "canceled"}),
        )
        broker.poll_interval = 0
        assert broker.cancel_stops("NVDA") == 1
        calls = [c.args for c in session.request.call_args_list]
        assert calls[1:] == [
            ("DELETE", "https://paper-api.alpaca.markets/v2/orders/s1"),
            ("GET", "https://paper-api.alpaca.markets/v2/orders/s1"),
            ("GET", "https://paper-api.alpaca.markets/v2/orders/s1"),
        ]

    def test_cancel_stops_times_out_if_cancel_never_lands(self):
        broker, _ = self._broker(
            _response(payload=[{"id": "s1", "side": "sell", "type": "stop"}]),
            _response(payload=None),
            _response(payload={"status": "pending_cancel"}),
        )
        broker.poll_interval = broker.cancel_timeout = 0
        with pytest.raises(BrokerError, match="still pending_cancel"):
            broker.cancel_stops("NVDA")

    def test_place_stop_payload(self):
        broker, session = self._broker(_response(payload={"id": "s"}))
        broker.place_stop("NVDA", 3, 92.0, "cid")
        assert session.request.call_args.kwargs["json"] == {
            "symbol": "NVDA", "qty": "3", "side": "sell", "type": "stop",
            "stop_price": "92.00", "time_in_force": "gtc", "client_order_id": "cid",
        }

    def test_latest_price_uses_data_api(self):
        broker, session = self._broker(_response(payload={"trade": {"p": 217.94}}))
        assert broker.latest_price("NVDA") == 217.94
        assert session.request.call_args.args == (
            "GET", "https://data.alpaca.markets/v2/stocks/NVDA/trades/latest",
        )

    def test_account_blocked_is_not_tradable(self):
        broker, _ = self._broker(_response(payload={
            "status": "ACTIVE", "trading_blocked": True, "buying_power": "10", "equity": "10",
        }))
        assert broker.account().tradable is False

    def test_calendar_and_positions(self):
        broker, _ = self._broker(
            _response(payload=[{"date": "2026-10-27"}]),
            _response(payload=[{"symbol": "nvda", "qty": "3", "market_value": "653.82",
                                "current_price": "217.94"}]),
        )
        assert broker.is_trading_day(TRADE_DATE) is True
        assert broker.positions() == {"NVDA": Position(3.0, 653.82, 217.94)}

    def test_http_error_raises_broker_error(self):
        broker, _ = self._broker(_response(403, {"message": "forbidden"}))
        with pytest.raises(BrokerError, match="403"):
            broker.account()
