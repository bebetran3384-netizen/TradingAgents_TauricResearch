"""Tests for the scheduled trading bot (config, sizing policy, runner, scheduler, Alpaca client)."""

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
import pytz

from tradingagents.bot.__main__ import next_run_at
from tradingagents.bot.broker import AccountState, AlpacaBroker, BrokerError, DryRunBroker
from tradingagents.bot.config import BotConfig, BotConfigError
from tradingagents.bot.policy import OrderPlan, plan_order
from tradingagents.bot.runner import Journal, run_once

TRADE_DATE = date(2026, 10, 27)  # a Tuesday


def _plan(rating, current=0.0, *, headroom=10_000.0, buying_power=10_000.0):
    return plan_order(
        "NVDA", rating, current,
        max_position_usd=1000.0, min_trade_usd=50.0,
        exposure_headroom_usd=headroom, buying_power_usd=buying_power,
    )


class FakeBroker:
    sends_orders = True

    def __init__(self, positions=None, buying_power=100_000.0, trading_day=True,
                 tradable=True, fail_on=()):
        self.positions = positions or {}
        self.buying_power = buying_power
        self.trading_day = trading_day
        self.tradable = tradable
        self.fail_on = set(fail_on)
        self.submitted = []

    def account(self):
        return AccountState(self.buying_power, self.buying_power, self.tradable, "ACTIVE")

    def position_values(self):
        return dict(self.positions)

    def is_trading_day(self, day):
        return self.trading_day

    def submit(self, plan, client_order_id):
        if plan.symbol in self.fail_on:
            raise BrokerError("rejected")
        self.submitted.append((plan, client_order_id))
        return {"id": f"order-{plan.symbol}"}


@pytest.fixture
def cfg(tmp_path):
    return BotConfig(
        watchlist=("NVDA", "AAPL", "MSFT"),
        mode="paper",
        max_position_usd=1000.0,
        max_total_exposure_usd=1500.0,
        min_trade_usd=50.0,
        state_dir=tmp_path,
        alpaca_key_id="k",
        alpaca_secret_key="s",
    )


def _ratings(**by_ticker):
    calls = []

    def analyze(ticker, trade_date):
        calls.append((ticker, trade_date))
        rating = by_ticker[ticker]
        if isinstance(rating, Exception):
            raise rating
        return rating

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
        ("TRADING_BOT_RUN_TIME", "25:00"),
        ("TRADING_BOT_TIMEZONE", "Mars/Olympus"),
    ])
    def test_invalid_values_rejected(self, key, value):
        with pytest.raises(BotConfigError):
            BotConfig.from_env({"TRADING_BOT_WATCHLIST": "NVDA", key: value})


# ---------------------------------------------------------------------------
# Sizing policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPlanOrder:
    def test_buy_from_flat_targets_full_position(self):
        plan, _ = _plan("Buy")
        assert plan == OrderPlan("NVDA", "buy", 1000.0, reason="Buy")

    def test_overweight_targets_three_quarters(self):
        plan, _ = _plan("Overweight", current=500.0)
        assert (plan.side, plan.notional_usd) == ("buy", 250.0)

    def test_hold_never_trades(self):
        assert _plan("Hold", current=300.0)[0] is None

    def test_buy_above_target_does_not_trim(self):
        assert _plan("Buy", current=1200.0)[0] is None

    def test_underweight_trims_to_quarter(self):
        plan, _ = _plan("Underweight", current=1000.0)
        assert (plan.side, plan.notional_usd, plan.close_position) == ("sell", 750.0, False)

    def test_underweight_below_target_does_not_buy(self):
        assert _plan("Underweight", current=100.0)[0] is None

    def test_sell_closes_position(self):
        plan, _ = _plan("Sell", current=640.0)
        assert plan.close_position and plan.side == "sell"

    def test_sell_with_no_position_is_noop(self):
        assert _plan("Sell")[0] is None

    def test_buy_capped_by_exposure_and_buying_power(self):
        assert _plan("Buy", headroom=300.0)[0].notional_usd == 300.0
        assert _plan("Buy", buying_power=120.0)[0].notional_usd == 120.0

    def test_buy_below_minimum_after_caps_is_skipped(self):
        assert _plan("Buy", headroom=20.0)[0] is None

    def test_short_position_left_alone(self):
        assert _plan("Sell", current=-500.0)[0] is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunOnce:
    def test_trades_each_ticker_and_respects_total_exposure(self, cfg):
        broker = FakeBroker()
        analyze = _ratings(NVDA="Buy", AAPL="Buy", MSFT="Hold")
        entries = run_once(cfg, TRADE_DATE, broker=broker, analyze=analyze)

        # 1500 total cap: NVDA gets 1000, AAPL only the remaining 500.
        assert [(p.symbol, p.notional_usd) for p, _ in broker.submitted] == [
            ("NVDA", 1000.0), ("AAPL", 500.0),
        ]
        assert broker.submitted[0][1] == "ta-2026-10-27-NVDA"
        assert [e["status"] for e in entries] == ["submitted", "submitted", "no_trade"]
        assert analyze.calls[0] == ("NVDA", "2026-10-27")

    def test_rerun_same_day_only_retries_failures(self, cfg):
        analyze = _ratings(NVDA="Buy", AAPL=RuntimeError("LLM down"), MSFT="Sell")
        first = run_once(cfg, TRADE_DATE, broker=FakeBroker(), analyze=analyze)
        assert [e["status"] for e in first] == ["submitted", "error", "no_trade"]

        broker = FakeBroker()
        analyze = _ratings(AAPL="Buy")
        run_once(cfg, TRADE_DATE, broker=broker, analyze=analyze)
        assert analyze.calls == [("AAPL", "2026-10-27")]
        assert [p.symbol for p, _ in broker.submitted] == ["AAPL"]

    def test_broker_rejection_is_journaled_and_run_continues(self, cfg):
        broker = FakeBroker(fail_on={"NVDA"})
        entries = run_once(cfg, TRADE_DATE, broker=broker,
                           analyze=_ratings(NVDA="Buy", AAPL="Buy", MSFT="Hold"))
        assert entries[0]["status"] == "error" and "rejected" in entries[0]["detail"]
        # The rejected buy did not consume exposure headroom.
        assert [(p.symbol, p.notional_usd) for p, _ in broker.submitted] == [("AAPL", 1000.0)]

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
        broker = FakeBroker(positions={"MSFT": 1200.0})
        run_once(cfg, TRADE_DATE, broker=broker,
                 analyze=_ratings(NVDA="Buy", AAPL="Buy", MSFT="Hold"))
        assert [(p.symbol, p.notional_usd) for p, _ in broker.submitted] == [("NVDA", 300.0)]

    def test_dry_run_records_without_sending(self, cfg):
        broker = DryRunBroker(buying_power=5000.0)
        entries = run_once(cfg, TRADE_DATE, broker=broker,
                           analyze=_ratings(NVDA="Buy", AAPL="Sell", MSFT="Hold"))
        assert [e["status"] for e in entries] == ["dry_run", "no_trade", "no_trade"]

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

    def test_notional_buy_order_payload(self):
        broker, session = self._broker(_response(payload={"id": "abc"}))
        broker.submit(OrderPlan("NVDA", "buy", 250.0), client_order_id="ta-2026-10-27-NVDA")
        method, url = session.request.call_args.args
        assert (method, url) == ("POST", "https://paper-api.alpaca.markets/v2/orders")
        assert session.request.call_args.kwargs["json"] == {
            "symbol": "NVDA", "notional": "250.00", "side": "buy", "type": "market",
            "time_in_force": "day", "client_order_id": "ta-2026-10-27-NVDA",
        }

    def test_close_uses_position_endpoint(self):
        broker, session = self._broker(_response(payload={"id": "x"}))
        broker.submit(OrderPlan("NVDA", "sell", 640.0, close_position=True), "id")
        assert session.request.call_args.args == (
            "DELETE", "https://paper-api.alpaca.markets/v2/positions/NVDA",
        )

    def test_account_blocked_is_not_tradable(self):
        broker, _ = self._broker(_response(payload={
            "status": "ACTIVE", "trading_blocked": True, "buying_power": "10", "equity": "10",
        }))
        assert broker.account().tradable is False

    def test_calendar_and_positions(self):
        broker, _ = self._broker(
            _response(payload=[{"date": "2026-10-27"}]),
            _response(payload=[{"symbol": "nvda", "market_value": "512.5"}]),
        )
        assert broker.is_trading_day(TRADE_DATE) is True
        assert broker.position_values() == {"NVDA": 512.5}

    def test_http_error_raises_broker_error(self):
        broker, _ = self._broker(_response(403, {"message": "forbidden"}))
        with pytest.raises(BrokerError, match="403"):
            broker.account()
