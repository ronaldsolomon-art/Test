#!/usr/bin/env python3
"""Paper-only 15-minute opening-range-breakout bot for EUR/USD and USD/JPY.

This program intentionally uses only OANDA's market-data candle endpoint. It contains
no order-placement, account-management, or trade-modification requests.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import date, datetime, time as clock_time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
RANGE_START = clock_time(9, 30)
RANGE_END = clock_time(9, 45)
ENTRY_START = clock_time(10, 0)


class BotError(RuntimeError):
    """A user-facing configuration or data error."""


@dataclass(frozen=True)
class Candle:
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @classmethod
    def from_oanda(cls, record: dict[str, Any]) -> "Candle":
        try:
            raw_time = str(record["time"]).replace("Z", "+00:00")
            mid = record["mid"]
            return cls(
                start=datetime.fromisoformat(raw_time).astimezone(UTC),
                open=Decimal(str(mid["o"])),
                high=Decimal(str(mid["h"])),
                low=Decimal(str(mid["l"])),
                close=Decimal(str(mid["c"])),
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise BotError(f"Malformed candle returned by market-data provider: {record!r}") from exc

    @property
    def new_york_start(self) -> datetime:
        return self.start.astimezone(NEW_YORK)


@dataclass
class PaperTrade:
    instrument: str
    side: str
    units: int
    entry_time: str
    entry_price: str
    stop_price: str
    target_price: str
    range_high: str
    range_low: str
    status: str = "OPEN"
    exit_time: str | None = None
    exit_price: str | None = None
    exit_reason: str | None = None
    pnl_quote_currency: str | None = None

    @classmethod
    def from_state(cls, payload: dict[str, Any]) -> "PaperTrade":
        return cls(**payload)

    def prices(self) -> tuple[Decimal, Decimal, Decimal]:
        return Decimal(self.entry_price), Decimal(self.stop_price), Decimal(self.target_price)


def decimal_text(value: Decimal, precision: int) -> str:
    return f"{value:.{precision}f}"


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as output:
        output.write(line + "\n")


def terminal_alert(level: str, message: str) -> None:
    timestamp = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    print(f"[{timestamp}] {level:<7} {message}", flush=True)


class OandaPracticeMarketData:
    """Read-only client for OANDA's practice-environment candle endpoint."""

    def __init__(self, base_url: str, token: str, timeout_seconds: int) -> None:
        if not base_url.startswith("https://") or "fxpractice.oanda.com" not in base_url:
            raise BotError(
                "For safety, market_data.base_url must be OANDA's HTTPS practice endpoint "
                "(https://api-fxpractice.oanda.com)."
            )
        if not token:
            raise BotError(
                "OANDA_API_TOKEN is not set. Create a practice-account personal access token and "
                "set it in your environment before running the bot."
            )
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def get_completed_m15_candles(self, instrument: str, count: int) -> list[Candle]:
        parameters = urllib.parse.urlencode(
            {"count": str(count), "granularity": "M15", "price": "M"}
        )
        url = f"{self.base_url}/v3/instruments/{instrument}/candles?{parameters}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "paper-orb-bot/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BotError(f"Market-data request failed with HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BotError(f"Market-data request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise BotError("Market-data provider returned invalid JSON.") from exc

        records = payload.get("candles")
        if not isinstance(records, list):
            raise BotError("Market-data response did not contain a candle list.")
        candles = [Candle.from_oanda(item) for item in records if item.get("complete") is True]
        return sorted(candles, key=lambda candle: candle.start)


class LocalRecorder:
    def __init__(self, state_path: Path, events_path: Path, trades_path: Path) -> None:
        self.state_path = state_path
        self.events_path = events_path
        self.trades_path = trades_path

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "pairs": {}}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BotError(f"The saved state file is invalid JSON: {self.state_path}") from exc
        if not isinstance(state, dict) or not isinstance(state.get("pairs", {}), dict):
            raise BotError("The saved state file has an unsupported structure.")
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        write_json_atomic(self.state_path, state)

    def event(self, event_type: str, instrument: str, message: str, **details: Any) -> None:
        payload = {
            "timestamp": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
            "type": event_type,
            "instrument": instrument,
            "message": message,
            "details": details,
        }
        append_jsonl(self.events_path, payload)
        terminal_alert(event_type, f"{instrument}: {message}")

    def record_closed_trade(self, trade: PaperTrade) -> None:
        self.trades_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.trades_path.exists() or self.trades_path.stat().st_size == 0
        with self.trades_path.open("a", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=list(asdict(trade).keys()))
            if write_header:
                writer.writeheader()
            writer.writerow(asdict(trade))


class OpeningRangeStrategy:
    """Deterministic paper-trading state machine for one instrument and one NY day."""

    def __init__(self, pair: dict[str, Any], bot_config: dict[str, Any], recorder: LocalRecorder) -> None:
        self.pair = pair
        self.instrument = str(pair["instrument"])
        self.point_size = Decimal(str(pair["point_size"]))
        self.units = int(pair["fixed_units"])
        self.precision = int(pair["display_precision"])
        self.target_points = Decimal(str(bot_config["target_points"]))
        self.max_trades = int(bot_config["max_trades_per_day"])
        self.entry_cutoff = clock_time.fromisoformat(str(bot_config["entry_cutoff_new_york"]))
        self.recorder = recorder
        if self.point_size <= 0 or self.units <= 0 or self.target_points <= 0:
            raise BotError(f"{self.instrument}: point_size, fixed_units, and target_points must be positive.")
        if self.max_trades != 1:
            raise BotError("This implementation intentionally supports one paper trade per pair per day only.")

    @staticmethod
    def _range_candles(candles: Iterable[Candle], session_date: date) -> list[Candle]:
        return [
            candle
            for candle in candles
            if candle.new_york_start.date() == session_date
            and candle.new_york_start.time() in (RANGE_START, RANGE_END)
        ]

    def establish_range(self, candles: Iterable[Candle], session_date: date, pair_state: dict[str, Any]) -> bool:
        range_candles = self._range_candles(candles, session_date)
        expected_times = {RANGE_START, RANGE_END}
        actual_times = {candle.new_york_start.time() for candle in range_candles}
        if not expected_times.issubset(actual_times):
            return False

        high = max(candle.high for candle in range_candles)
        low = min(candle.low for candle in range_candles)
        high_text = decimal_text(high, self.precision)
        low_text = decimal_text(low, self.precision)
        stored_range = pair_state.get("range")
        if not isinstance(stored_range, dict) or stored_range.get("high") != high_text or stored_range.get("low") != low_text:
            new_range = {
                "high": high_text,
                "low": low_text,
                "established_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
            }
            pair_state["range"] = new_range
            self.recorder.event(
                "RANGE",
                self.instrument,
                "Opening range established from the 09:30 and 09:45 New York candles.",
                high=new_range["high"],
                low=new_range["low"],
            )
        return True

    def process_candle(self, candle: Candle, pair_state: dict[str, Any]) -> None:
        local_start = candle.new_york_start
        if local_start.time() < ENTRY_START or local_start.time() > self.entry_cutoff:
            return
        opening_range = pair_state.get("range")
        if not opening_range:
            return

        existing_payload = pair_state.get("trade")
        if existing_payload:
            trade = PaperTrade.from_state(existing_payload)
            if trade.status == "OPEN":
                self._evaluate_exit(candle, trade)
                pair_state["trade"] = asdict(trade)
            return

        range_high = Decimal(opening_range["high"])
        range_low = Decimal(opening_range["low"])
        side: str | None = None
        if candle.close > range_high:
            side = "LONG"
        elif candle.close < range_low:
            side = "SHORT"
        if side is None:
            return

        distance = self.target_points * self.point_size
        if side == "LONG":
            stop = range_low
            target = candle.close + distance
        else:
            stop = range_high
            target = candle.close - distance

        trade = PaperTrade(
            instrument=self.instrument,
            side=side,
            units=self.units,
            entry_time=candle.start.isoformat().replace("+00:00", "Z"),
            entry_price=decimal_text(candle.close, self.precision),
            stop_price=decimal_text(stop, self.precision),
            target_price=decimal_text(target, self.precision),
            range_high=decimal_text(range_high, self.precision),
            range_low=decimal_text(range_low, self.precision),
        )
        pair_state["trade"] = asdict(trade)
        self.recorder.event(
            "ENTRY",
            self.instrument,
            f"{side} paper trade opened on a completed M15 close.",
            entry_price=trade.entry_price,
            stop_price=trade.stop_price,
            target_price=trade.target_price,
            target_points=str(self.target_points),
            point_size=str(self.point_size),
            units=trade.units,
            candle_start=trade.entry_time,
        )

    def _evaluate_exit(self, candle: Candle, trade: PaperTrade) -> None:
        entry, stop, target = trade.prices()
        reason: str | None = None
        exit_price: Decimal | None = None
        if trade.side == "LONG":
            # Stop first is deliberately conservative when both levels occur inside one M15 bar.
            if candle.low <= stop:
                reason, exit_price = "STOP", stop
            elif candle.high >= target:
                reason, exit_price = "TARGET", target
        else:
            if candle.high >= stop:
                reason, exit_price = "STOP", stop
            elif candle.low <= target:
                reason, exit_price = "TARGET", target
        if reason is None or exit_price is None:
            return

        if trade.side == "LONG":
            pnl = (exit_price - entry) * Decimal(trade.units)
        else:
            pnl = (entry - exit_price) * Decimal(trade.units)
        quote_currency = trade.instrument.split("_")[1]
        trade.status = "CLOSED"
        trade.exit_time = candle.start.isoformat().replace("+00:00", "Z")
        trade.exit_price = decimal_text(exit_price, self.precision)
        trade.exit_reason = reason
        trade.pnl_quote_currency = decimal_text(pnl, self.precision)
        self.recorder.record_closed_trade(trade)
        self.recorder.event(
            "EXIT",
            self.instrument,
            f"{trade.side} paper trade closed at its {reason}.",
            exit_price=trade.exit_price,
            pnl_quote_currency=trade.pnl_quote_currency,
            quote_currency=quote_currency,
            candle_start=trade.exit_time,
        )


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except FileNotFoundError as exc:
        raise BotError(f"Configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BotError(f"Invalid TOML configuration: {exc}") from exc

    required_sections = {"bot", "market_data", "logging", "pairs"}
    missing = required_sections.difference(config)
    if missing:
        raise BotError(f"Configuration is missing required sections: {', '.join(sorted(missing))}")
    if not isinstance(config["pairs"], list) or not config["pairs"]:
        raise BotError("Configuration must define at least one pair in [[pairs]].")
    return config


def candles_for_new_york_date(candles: Iterable[Candle], session_date: date) -> list[Candle]:
    return [candle for candle in candles if candle.new_york_start.date() == session_date]


def make_fresh_pair_state(session_date: date) -> dict[str, Any]:
    return {"session_date": session_date.isoformat(), "range": None, "trade": None, "last_processed": None}


def run_pair(
    pair: dict[str, Any],
    config: dict[str, Any],
    client: OandaPracticeMarketData,
    recorder: LocalRecorder,
    state: dict[str, Any],
    replay_today: bool,
) -> None:
    instrument = str(pair["instrument"])
    now_new_york = datetime.now(tz=NEW_YORK)
    session_date = now_new_york.date()
    if now_new_york.isoweekday() > 5:
        recorder.event("STATUS", instrument, "No new paper trades on Saturday or Sunday New York time.")
        return

    pair_states = state.setdefault("pairs", {})
    pair_state = pair_states.get(instrument)
    if not isinstance(pair_state, dict) or pair_state.get("session_date") != session_date.isoformat() or replay_today:
        pair_state = make_fresh_pair_state(session_date)
        pair_states[instrument] = pair_state
        if replay_today:
            recorder.event("STATUS", instrument, "Replaying completed candles for the current New York session.")

    history_count = int(config["bot"]["history_candle_count"])
    candles = candles_for_new_york_date(client.get_completed_m15_candles(instrument, history_count), session_date)
    if not candles:
        recorder.event("STATUS", instrument, "No completed M15 candles returned for the current New York date.")
        return

    strategy = OpeningRangeStrategy(pair, config["bot"], recorder)
    if not strategy.establish_range(candles, session_date, pair_state):
        recorder.event("STATUS", instrument, "Waiting for both opening-range candles to complete.")
        return

    if replay_today:
        candidates = candles
    elif pair_state.get("last_processed"):
        checkpoint = parse_iso_datetime(str(pair_state["last_processed"]))
        candidates = [candle for candle in candles if candle.start > checkpoint]
    else:
        # A first live run evaluates only the most recently completed bar; starting the bot
        # before 10:00 New York time avoids missing an earlier session signal.
        candidates = [candles[-1]]

    for candle in candidates:
        strategy.process_candle(candle, pair_state)
        pair_state["last_processed"] = candle.start.isoformat().replace("+00:00", "Z")

    if candidates:
        recorder.event(
            "STATUS",
            instrument,
            "Processed completed M15 candle data.",
            processed_candles=len(candidates),
            last_processed=pair_state["last_processed"],
        )


def run_once(config_path: Path, replay_today: bool) -> None:
    config = load_config(config_path)
    base_dir = config_path.parent
    logging_config = config["logging"]
    recorder = LocalRecorder(
        state_path=base_dir / str(logging_config["state_file"]),
        events_path=base_dir / str(logging_config["events_file"]),
        trades_path=base_dir / str(logging_config["trades_file"]),
    )
    state = recorder.load_state()
    market_config = config["market_data"]
    client = OandaPracticeMarketData(
        base_url=str(market_config["base_url"]),
        token=os.getenv("OANDA_API_TOKEN", "").strip(),
        timeout_seconds=int(market_config["timeout_seconds"]),
    )
    for pair in config["pairs"]:
        run_pair(pair, config, client, recorder, state, replay_today)
    recorder.save_state(state)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Paper-only 15-minute opening-range-breakout alerts for EUR/USD and USD/JPY."
    )
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="Path to the TOML configuration file.")
    parser.add_argument("--once", action="store_true", help="Poll once, process available data, then exit.")
    parser.add_argument(
        "--replay-today",
        action="store_true",
        help="Rebuild today's paper simulation from completed current-session candles. Intended for testing only.",
    )
    arguments = parser.parse_args()
    if arguments.replay_today and not arguments.once:
        raise BotError("--replay-today is a single-run audit mode and must be used together with --once.")

    config_path = arguments.config.expanduser().resolve()
    config = load_config(config_path)
    poll_seconds = int(config["bot"]["poll_interval_seconds"])
    if poll_seconds < 15:
        raise BotError("bot.poll_interval_seconds must be at least 15 seconds.")

    terminal_alert("NOTICE", "Paper-only bot started. It performs read-only market-data requests and cannot place orders.")
    should_stop = False

    def stop_handler(_signum: int, _frame: Any) -> None:
        nonlocal should_stop
        should_stop = True
        terminal_alert("NOTICE", "Stop requested; the bot will exit after the current cycle.")

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    while not should_stop:
        try:
            run_once(config_path, arguments.replay_today)
        except BotError as exc:
            terminal_alert("ERROR", str(exc))
        except Exception as exc:  # Defensive boundary so a transient fault does not silently kill a live alert loop.
            terminal_alert("ERROR", f"Unexpected failure: {exc.__class__.__name__}: {exc}")

        if arguments.once or should_stop:
            break
        time.sleep(poll_seconds)

    terminal_alert("NOTICE", "Paper-only bot stopped.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BotError as exc:
        terminal_alert("ERROR", str(exc))
        raise SystemExit(2)
