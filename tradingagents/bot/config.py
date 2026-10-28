"""Environment-driven configuration for the trading bot.

Every setting comes from a ``TRADING_BOT_*`` (or ``ALPACA_*``) env var so the
bot can be configured entirely from ``.env`` in Docker. Defaults are chosen to
be safe: ``dry_run`` mode and small position caps.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytz

MODES = ("dry_run", "paper", "live")

# Typing this exact value is the deliberate step that arms real-money trading.
LIVE_CONFIRMATION = "yes"

_RUN_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class BotConfigError(ValueError):
    """Raised when the bot's environment configuration is invalid."""


@dataclass(frozen=True)
class BotConfig:
    watchlist: tuple[str, ...]
    mode: str = "dry_run"
    max_position_usd: float = 1000.0
    max_total_exposure_usd: float = 5000.0
    min_trade_usd: float = 50.0
    run_time: str = "16:30"
    timezone: str = "America/New_York"
    state_dir: Path = Path.home() / ".tradingagents" / "bot"
    alpaca_key_id: str | None = None
    alpaca_secret_key: str | None = None

    @property
    def kill_switch_path(self) -> Path:
        return self.state_dir / "STOP"

    @property
    def journal_path(self) -> Path:
        return self.state_dir / "journal.jsonl"

    @property
    def run_hour_minute(self) -> tuple[int, int]:
        hour, minute = self.run_time.split(":")
        return int(hour), int(minute)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BotConfig:
        env = os.environ if env is None else env

        watchlist = tuple(
            dict.fromkeys(
                t.strip().upper()
                for t in env.get("TRADING_BOT_WATCHLIST", "").split(",")
                if t.strip()
            )
        )
        state_dir = env.get("TRADING_BOT_STATE_DIR") or Path.home() / ".tradingagents" / "bot"
        cfg = cls(
            watchlist=watchlist,
            mode=env.get("TRADING_BOT_MODE", "dry_run").strip().lower(),
            max_position_usd=_float(env, "TRADING_BOT_MAX_POSITION_USD", 1000.0),
            max_total_exposure_usd=_float(env, "TRADING_BOT_MAX_TOTAL_EXPOSURE_USD", 5000.0),
            min_trade_usd=_float(env, "TRADING_BOT_MIN_TRADE_USD", 50.0),
            run_time=env.get("TRADING_BOT_RUN_TIME", "16:30").strip(),
            timezone=env.get("TRADING_BOT_TIMEZONE", "America/New_York").strip(),
            state_dir=Path(state_dir).expanduser(),
            alpaca_key_id=env.get("ALPACA_API_KEY_ID") or None,
            alpaca_secret_key=env.get("ALPACA_API_SECRET_KEY") or None,
        )
        cfg._validate(env)
        return cfg

    def _validate(self, env: Mapping[str, str]) -> None:
        if not self.watchlist:
            raise BotConfigError("TRADING_BOT_WATCHLIST is empty; set e.g. NVDA,AAPL,MSFT")
        if self.mode not in MODES:
            raise BotConfigError(f"TRADING_BOT_MODE must be one of {MODES}, got {self.mode!r}")
        if self.mode in ("paper", "live") and not (self.alpaca_key_id and self.alpaca_secret_key):
            raise BotConfigError(
                f"{self.mode} mode needs ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY"
            )
        if (
            self.mode == "live"
            and env.get("TRADING_BOT_CONFIRM_LIVE", "").strip().lower() != LIVE_CONFIRMATION
        ):
            raise BotConfigError(
                "live mode places real-money orders; set TRADING_BOT_CONFIRM_LIVE=yes to arm it"
            )
        for name in ("max_position_usd", "max_total_exposure_usd"):
            if getattr(self, name) <= 0:
                raise BotConfigError(f"{name} must be positive")
        if self.min_trade_usd < 1:
            raise BotConfigError("min_trade_usd must be at least 1 (Alpaca's notional minimum)")
        if self.max_total_exposure_usd < self.max_position_usd:
            raise BotConfigError("max_total_exposure_usd must be >= max_position_usd")
        if not _RUN_TIME_RE.match(self.run_time):
            raise BotConfigError(f"TRADING_BOT_RUN_TIME must be HH:MM, got {self.run_time!r}")
        try:
            pytz.timezone(self.timezone)
        except pytz.UnknownTimeZoneError as exc:
            raise BotConfigError(f"unknown TRADING_BOT_TIMEZONE {self.timezone!r}") from exc


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise BotConfigError(f"{key} must be a number, got {raw!r}") from exc
