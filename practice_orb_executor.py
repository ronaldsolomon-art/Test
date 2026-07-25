#!/usr/bin/env python3
"""OANDA practice-only 15-minute New York opening-range breakout executor.

This is intentionally a separate program from ``paper_orb_bot.py``. It is
hard-wired to OANDA's HTTPS practice endpoint and refuses any other base URL.
It has no live-account mode. Order submission additionally requires the
``--confirm-practice-orders`` flag at every start.

The program is designed for controlled practice-account testing only. It never
prints the access token and it does not support funding, transfers, account
configuration, leverage changes, automatic force-closes, or live endpoints.
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

# Shared, read-only candle parsing and local helper functions. Importing this
# module does not start the paper bot because it uses a normal __main__ guard.
from paper_orb_bot import (  # noqa: E402
    BotError,
    Candle,
    NEW_YORK,
    UTC,
    RANGE_START,
    RANGE_END,
    ENTRY_START,
    OandaPracticeMarketData,
    append_jsonl,
    decimal_text,
    parse_iso_datetime,
    terminal_alert,
    write_json_atomic,
)


PRACTICE_BASE_URL = "https://api-fxpractice.oanda.com"
PROGRAM_VERSION = "1.0"


class OandaApiError(BotError):
    """An OANDA API response whose status and body are safe to log."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"OANDA API request failed with HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class SubmissionUnknown(BotError):
    """A POST might have reached OANDA; block retries until a human checks it."""


class InvalidSignalParameters(BotError):
    """A breakout was detected but its computed stop/target/bound is unusable."""


@dataclass(frozen=True)
class PairSettings:
    instrument: str
    display_precision: int
    point_size: Decimal
    units: int


@dataclass(frozen=True)
class ExecutionSettings:
    account_id: str
    timeout_seconds: int
    max_total_open_trades: int
    max_units_per_order: int
    min_margin_available: Decimal
    max_entry_deviation_points: Decimal


@dataclass(frozen=True)
class Signal:
    instrument: str
    side: str
    candle_start: str
    entry_reference: str
    stop_price: str
    target_price: str
    units: int
    price_bound: str
    signal_key: str


def utc_text(value: datetime | None = None) -> str:
    moment = value or datetime.now(tz=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def masked_account_id(account_id: str) -> str:
    if len(account_id) <= 4:
        return "****"
    return f"***{account_id[-4:]}"


def require_exact_practice_url(value: str) -> str:
    normalized = value.rstrip("/")
    if normalized != PRACTICE_BASE_URL:
        raise BotError(
            "For safety, market_data.base_url must be exactly "
            f"{PRACTICE_BASE_URL!r}. This executor has no live-account mode."
        )
    return normalized


def load_practice_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except FileNotFoundError as exc:
        raise BotError(f"Configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BotError(f"Invalid TOML configuration: {exc}") from exc

    required_sections = {"bot", "market_data", "execution", "logging", "pairs"}
    missing = required_sections.difference(config)
    if missing:
        raise BotError(f"Configuration is missing required sections: {', '.join(sorted(missing))}")
    if not isinstance(config["pairs"], list) or not config["pairs"]:
        raise BotError("Configuration must define at least one pair in [[pairs]].")
    if str(config["execution"].get("environment", "")) != "practice_only":
        raise BotError("execution.environment must be exactly 'practice_only'.")
    require_exact_practice_url(str(config["market_data"].get("base_url", "")))
    return config


def parse_pair_settings(raw: dict[str, Any]) -> PairSettings:
    try:
        pair = PairSettings(
            instrument=str(raw["instrument"]),
            display_precision=int(raw["display_precision"]),
            point_size=Decimal(str(raw["point_size"])),
            units=int(raw["fixed_units"]),
        )
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise BotError(f"Invalid pair configuration: {raw!r}") from exc
    if pair.point_size <= 0 or pair.units <= 0 or pair.display_precision < 0:
        raise BotError(f"{pair.instrument}: point_size, fixed_units, and display_precision must be positive.")
    return pair


def parse_execution_settings(config: dict[str, Any]) -> ExecutionSettings:
    execution = config["execution"]
    account_env_name = str(execution.get("account_id_env", "OANDA_PRACTICE_ACCOUNT_ID"))
    account_id = os.getenv(account_env_name, "").strip()
    if not account_id:
        raise BotError(
            f"{account_env_name} is not set. Set your own OANDA practice account ID in the environment; "
            "do not put it in the configuration file."
        )
    try:
        settings = ExecutionSettings(
            account_id=account_id,
            timeout_seconds=int(config["market_data"]["timeout_seconds"]),
            max_total_open_trades=int(execution["max_total_open_trades"]),
            max_units_per_order=int(execution["max_units_per_order"]),
            min_margin_available=Decimal(str(execution["min_margin_available"])),
            max_entry_deviation_points=Decimal(str(execution["max_entry_deviation_points"])),
        )
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise BotError("Invalid [execution] or [market_data] settings.") from exc
    if (
        settings.timeout_seconds <= 0
        or settings.max_total_open_trades <= 0
        or settings.max_units_per_order <= 0
        or settings.min_margin_available < 0
        or settings.max_entry_deviation_points <= 0
    ):
        raise BotError("Execution limits must be positive; min_margin_available may be zero but not negative.")
    return settings


class PracticeRecorder:
    """Durable local state and sanitized practice-order audit records."""

    def __init__(
        self,
        state_path: Path,
        events_path: Path,
        orders_path: Path,
        trades_path: Path,
    ) -> None:
        self.state_path = state_path
        self.events_path = events_path
        self.orders_path = orders_path
        self.trades_path = trades_path

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "pairs": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BotError(f"Practice executor state is invalid JSON: {self.state_path}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("pairs"), dict):
            raise BotError("Practice executor state has an unsupported structure.")
        return payload

    def save_state(self, payload: dict[str, Any]) -> None:
        write_json_atomic(self.state_path, payload)

    def event(self, event_type: str, instrument: str, message: str, **details: Any) -> None:
        payload = {
            "timestamp": utc_text(),
            "type": event_type,
            "instrument": instrument,
            "message": message,
            "details": details,
        }
        append_jsonl(self.events_path, payload)
        terminal_alert(event_type, f"{instrument}: {message}")

    @staticmethod
    def _append_csv(path: Path, row: dict[str, Any], fields: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        header_required = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            if header_required:
                writer.writeheader()
            writer.writerow(row)

    def record_order(self, row: dict[str, Any]) -> None:
        fields = [
            "timestamp",
            "instrument",
            "side",
            "signal_key",
            "submission_status",
            "order_id",
            "fill_transaction_id",
            "trade_id",
            "fill_price",
            "requested_units",
            "stop_price",
            "target_price",
            "price_bound",
            "request_id",
            "error_code",
            "error_message",
        ]
        self._append_csv(self.orders_path, row, fields)

    def record_trade_snapshot(self, row: dict[str, Any]) -> None:
        fields = [
            "checked_at",
            "instrument",
            "trade_id",
            "state",
            "open_time",
            "close_time",
            "entry_price",
            "current_units",
            "unrealized_pl",
            "realized_pl",
        ]
        self._append_csv(self.trades_path, row, fields)


class OandaPracticeExecutionClient:
    """Small, guarded OANDA v20 client limited to practice-account actions."""

    def __init__(self, base_url: str, token: str, account_id: str, timeout_seconds: int) -> None:
        self.base_url = require_exact_practice_url(base_url)
        if not token:
            raise BotError(
                "OANDA_API_TOKEN is not set. Create a practice-account token and set it in your shell; "
                "never store it in a file."
            )
        if not account_id:
            raise BotError("A practice account ID is required.")
        self.token = token
        self.account_id = account_id
        self.timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not path.startswith("/"):
            raise BotError("Internal safety error: OANDA path must begin with '/'.")
        url = f"{self.base_url}{path}"
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": f"practice-orb-executor/{PROGRAM_VERSION}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise OandaApiError(exc.code, detail) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if method == "POST":
                raise SubmissionUnknown(
                    "The order request encountered a transport error after submission began. "
                    "Check the OANDA practice account manually before retrying; this bot will block same-day retries."
                ) from exc
            raise BotError(f"OANDA practice request failed: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BotError("OANDA practice endpoint returned invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise BotError("OANDA practice endpoint returned an unexpected JSON structure.")
        return parsed

    def list_authorized_accounts(self) -> list[str]:
        response = self._request("GET", "/v3/accounts")
        accounts = response.get("accounts")
        if not isinstance(accounts, list):
            raise BotError("OANDA account-list response has no account list.")
        return [str(item.get("id")) for item in accounts if isinstance(item, dict) and item.get("id")]

    def account_summary(self) -> dict[str, Any]:
        response = self._request("GET", f"/v3/accounts/{urllib.parse.quote(self.account_id, safe='')}/summary")
        account = response.get("account")
        if not isinstance(account, dict):
            raise BotError("OANDA practice account-summary response has no account object.")
        return account

    def account_instruments(self) -> list[dict[str, Any]]:
        response = self._request("GET", f"/v3/accounts/{urllib.parse.quote(self.account_id, safe='')}/instruments")
        instruments = response.get("instruments")
        if not isinstance(instruments, list):
            raise BotError("OANDA instrument response has no instrument list.")
        return [item for item in instruments if isinstance(item, dict)]

    def price(self, instrument: str) -> dict[str, Any]:
        parameters = urllib.parse.urlencode({"instruments": instrument})
        response = self._request(
            "GET",
            f"/v3/accounts/{urllib.parse.quote(self.account_id, safe='')}/pricing?{parameters}",
        )
        prices = response.get("prices")
        if not isinstance(prices, list) or not prices or not isinstance(prices[0], dict):
            raise BotError(f"OANDA did not return a usable practice price for {instrument}.")
        return prices[0]

    def open_trades(self, instrument: str) -> list[dict[str, Any]]:
        parameters = urllib.parse.urlencode({"instrument": instrument})
        response = self._request(
            "GET",
            f"/v3/accounts/{urllib.parse.quote(self.account_id, safe='')}/openTrades?{parameters}",
        )
        trades = response.get("trades")
        if not isinstance(trades, list):
            raise BotError("OANDA open-trades response has no trade list.")
        return [item for item in trades if isinstance(item, dict)]

    def pending_orders(self, instrument: str) -> list[dict[str, Any]]:
        parameters = urllib.parse.urlencode({"state": "PENDING", "instrument": instrument})
        response = self._request(
            "GET",
            f"/v3/accounts/{urllib.parse.quote(self.account_id, safe='')}/orders?{parameters}",
        )
        orders = response.get("orders")
        if not isinstance(orders, list):
            raise BotError("OANDA pending-orders response has no order list.")
        return [item for item in orders if isinstance(item, dict)]

    def trade(self, trade_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/v3/accounts/{urllib.parse.quote(self.account_id, safe='')}/trades/{urllib.parse.quote(trade_id, safe='')}",
        )
        trade = response.get("trade")
        if not isinstance(trade, dict):
            raise BotError(f"OANDA did not return trade details for trade {trade_id}.")
        return trade

    def create_market_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        # This is the sole POST in the program. The fixed base URL was checked
        # at construction and the caller must also provide an arming flag.
        return self._request(
            "POST",
            f"/v3/accounts/{urllib.parse.quote(self.account_id, safe='')}/orders",
            {"order": order_payload},
        )


def fresh_pair_state(session_date: date) -> dict[str, Any]:
    return {
        "session_date": session_date.isoformat(),
        "range": None,
        "last_processed": None,
        "submission": None,
        "trade_snapshot": None,
    }


def ny_session_candles(candles: Iterable[Candle], session_date: date) -> list[Candle]:
    return [candle for candle in candles if candle.new_york_start.date() == session_date]


def range_candles(candles: Iterable[Candle], session_date: date) -> list[Candle]:
    return [
        candle
        for candle in candles
        if candle.new_york_start.date() == session_date
        and candle.new_york_start.time() in (RANGE_START, RANGE_END)
    ]


class PracticeExecutor:
    def __init__(
        self,
        config: dict[str, Any],
        recorder: PracticeRecorder,
        market_data: OandaPracticeMarketData,
        execution_client: OandaPracticeExecutionClient,
        execution_settings: ExecutionSettings,
        armed: bool,
    ) -> None:
        self.config = config
        self.recorder = recorder
        self.market_data = market_data
        self.client = execution_client
        self.execution_settings = execution_settings
        self.armed = armed
        self.target_points = Decimal(str(config["bot"]["target_points"]))
        self.entry_cutoff = clock_time.fromisoformat(str(config["bot"]["entry_cutoff_new_york"]))
        self.history_count = int(config["bot"]["history_candle_count"])
        self.pairs = [parse_pair_settings(item) for item in config["pairs"]]
        if self.target_points <= 0 or self.history_count < 8:
            raise BotError("bot.target_points must be positive and history_candle_count must be at least 8.")
        self.instrument_metadata: dict[str, dict[str, Any]] = {}

    def initialize(self) -> None:
        authorized = self.client.list_authorized_accounts()
        if self.execution_settings.account_id not in authorized:
            raise BotError(
                f"The selected practice account ({masked_account_id(self.execution_settings.account_id)}) "
                "is not authorized by the supplied token."
            )
        raw_instruments = self.client.account_instruments()
        available = {str(item.get("name")): item for item in raw_instruments if item.get("name")}
        for pair in self.pairs:
            metadata = available.get(pair.instrument)
            if metadata is None:
                raise BotError(f"{pair.instrument} is not available to the selected OANDA practice account.")
            try:
                minimum = Decimal(str(metadata["minimumTradeSize"]))
                maximum = Decimal(str(metadata["maximumOrderUnits"]))
                display_precision = int(metadata["displayPrecision"])
                units_precision = int(metadata["tradeUnitsPrecision"])
            except (KeyError, ValueError, InvalidOperation) as exc:
                raise BotError(f"{pair.instrument}: incomplete OANDA instrument metadata.") from exc
            if display_precision != pair.display_precision:
                raise BotError(
                    f"{pair.instrument}: config display_precision={pair.display_precision} does not match OANDA "
                    f"practice metadata ({display_precision}); correct the config before arming orders."
                )
            if units_precision != 0:
                raise BotError(
                    f"{pair.instrument}: this executor supports only integer trade units, but OANDA reports "
                    f"tradeUnitsPrecision={units_precision}."
                )
            if Decimal(pair.units) < minimum or Decimal(pair.units) > maximum:
                raise BotError(
                    f"{pair.instrument}: fixed_units={pair.units} violates OANDA practice account limits "
                    f"({minimum} to {maximum})."
                )
            if pair.units > self.execution_settings.max_units_per_order:
                raise BotError(
                    f"{pair.instrument}: fixed_units={pair.units} exceeds execution.max_units_per_order="
                    f"{self.execution_settings.max_units_per_order}."
                )
            self.instrument_metadata[pair.instrument] = metadata
        mode = "ARMED" if self.armed else "DRY-RUN"
        terminal_alert(
            "NOTICE",
            f"Practice executor initialization complete ({mode}); account {masked_account_id(self.execution_settings.account_id)}. "
            "This program refuses any live URL.",
        )

    def establish_range(self, pair: PairSettings, candles: Iterable[Candle], session_date: date, pair_state: dict[str, Any]) -> bool:
        candidates = range_candles(candles, session_date)
        expected = {RANGE_START, RANGE_END}
        actual = {candle.new_york_start.time() for candle in candidates}
        if not expected.issubset(actual):
            return False
        high = max(candle.high for candle in candidates)
        low = min(candle.low for candle in candidates)
        high_text = decimal_text(high, pair.display_precision)
        low_text = decimal_text(low, pair.display_precision)
        saved = pair_state.get("range")
        if not isinstance(saved, dict) or saved.get("high") != high_text or saved.get("low") != low_text:
            pair_state["range"] = {
                "high": high_text,
                "low": low_text,
                "established_at": utc_text(),
            }
            self.recorder.event(
                "RANGE",
                pair.instrument,
                "Opening range established from the 09:30 and 09:45 New York candles.",
                high=high_text,
                low=low_text,
            )
        return True

    def signal_from_candle(self, pair: PairSettings, candle: Candle, pair_state: dict[str, Any]) -> Signal | None:
        local_start = candle.new_york_start
        if local_start.time() < ENTRY_START or local_start.time() > self.entry_cutoff:
            return None
        opening_range = pair_state.get("range")
        if not isinstance(opening_range, dict):
            return None
        range_high = Decimal(str(opening_range["high"]))
        range_low = Decimal(str(opening_range["low"]))
        if candle.close > range_high:
            side = "LONG"
        elif candle.close < range_low:
            side = "SHORT"
        else:
            return None

        distance = self.target_points * pair.point_size
        deviation = self.execution_settings.max_entry_deviation_points * pair.point_size
        if side == "LONG":
            stop = range_low
            target = candle.close + distance
            bound = candle.close + deviation
            valid = stop < candle.close < target
        else:
            stop = range_high
            target = candle.close - distance
            bound = candle.close - deviation
            valid = target < candle.close < stop
        if not valid or bound <= 0 or target <= 0:
            raise InvalidSignalParameters(
                f"{pair.instrument}: calculated stop, target, or entry bound is invalid; no order sent."
            )
        candle_text = candle.start.isoformat().replace("+00:00", "Z")
        key = f"practice-orb-{pair.instrument}-{local_start.date().isoformat()}-{side}-{candle_text}"
        return Signal(
            instrument=pair.instrument,
            side=side,
            candle_start=candle_text,
            entry_reference=decimal_text(candle.close, pair.display_precision),
            stop_price=decimal_text(stop, pair.display_precision),
            target_price=decimal_text(target, pair.display_precision),
            units=pair.units,
            price_bound=decimal_text(bound, pair.display_precision),
            signal_key=key,
        )

    def _preflight(self, pair: PairSettings) -> tuple[bool, str]:
        summary = self.client.account_summary()
        try:
            margin_available = Decimal(str(summary["marginAvailable"]))
            open_count = int(summary["openTradeCount"])
            pending_count = int(summary["pendingOrderCount"])
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise BotError("OANDA practice account summary lacks required risk fields.") from exc
        if margin_available < self.execution_settings.min_margin_available:
            return False, "margin_available_below_configured_minimum"
        if open_count + pending_count >= self.execution_settings.max_total_open_trades:
            return False, "maximum_total_open_or_pending_trades_reached"
        price = self.client.price(pair.instrument)
        if str(price.get("status", "")) != "tradeable":
            return False, "instrument_not_tradeable"
        if self.client.open_trades(pair.instrument):
            return False, "existing_open_trade_for_instrument"
        if self.client.pending_orders(pair.instrument):
            return False, "existing_pending_order_for_instrument"
        return True, "ok"

    @staticmethod
    def market_order_payload(signal: Signal) -> dict[str, Any]:
        signed_units = signal.units if signal.side == "LONG" else -signal.units
        return {
            "type": "MARKET",
            "instrument": signal.instrument,
            "units": str(signed_units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "priceBound": signal.price_bound,
            "stopLossOnFill": {"timeInForce": "GTC", "price": signal.stop_price},
            "takeProfitOnFill": {"timeInForce": "GTC", "price": signal.target_price},
        }

    @staticmethod
    def _order_record(signal: Signal, status: str, response: dict[str, Any] | None = None, **error: str) -> dict[str, Any]:
        response = response or {}
        create = response.get("orderCreateTransaction") if isinstance(response.get("orderCreateTransaction"), dict) else {}
        fill = response.get("orderFillTransaction") if isinstance(response.get("orderFillTransaction"), dict) else {}
        opened = fill.get("tradeOpened") if isinstance(fill.get("tradeOpened"), dict) else {}
        return {
            "timestamp": utc_text(),
            "instrument": signal.instrument,
            "side": signal.side,
            "signal_key": signal.signal_key,
            "submission_status": status,
            "order_id": str(fill.get("orderID") or create.get("id") or ""),
            "fill_transaction_id": str(fill.get("id") or ""),
            "trade_id": str(opened.get("tradeID") or ""),
            "fill_price": str(fill.get("price") or ""),
            "requested_units": str(signal.units if signal.side == "LONG" else -signal.units),
            "stop_price": signal.stop_price,
            "target_price": signal.target_price,
            "price_bound": signal.price_bound,
            "request_id": str(fill.get("requestID") or create.get("requestID") or ""),
            "error_code": error.get("error_code", ""),
            "error_message": error.get("error_message", ""),
        }

    def submit_signal(self, pair: PairSettings, signal: Signal, pair_state: dict[str, Any], state: dict[str, Any]) -> None:
        if not self.armed:
            # Record the first confirmed signal even in dry-run mode. This
            # preserves the one-signal-per-pair-per-day rule during replay and
            # prevents the audit log from presenting multiple hypothetical
            # entries for the same instrument and session.
            pair_state["submission"] = {
                "status": "DRYRUN",
                "signal_key": signal.signal_key,
                "observed_at": utc_text(),
                "side": signal.side,
                "entry_reference": signal.entry_reference,
                "stop_price": signal.stop_price,
                "target_price": signal.target_price,
            }
            self.recorder.save_state(state)
            self.recorder.event(
                "DRYRUN",
                pair.instrument,
                f"{signal.side} signal observed; no practice order submitted without --confirm-practice-orders.",
                signal_key=signal.signal_key,
                entry_reference=signal.entry_reference,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
            )
            return

        allowed, reason = self._preflight(pair)
        if not allowed:
            pair_state["submission"] = {
                "status": "BLOCKED",
                "signal_key": signal.signal_key,
                "reason": reason,
                "blocked_at": utc_text(),
            }
            self.recorder.save_state(state)
            self.recorder.event("BLOCKED", pair.instrument, "Practice order blocked by preflight safeguard.", reason=reason)
            return

        # Persist a marker before the only POST call. A crash or transport error
        # after this point blocks same-day retries rather than risking duplicates.
        pair_state["submission"] = {
            "status": "SUBMISSION_STARTED",
            "signal_key": signal.signal_key,
            "started_at": utc_text(),
            "side": signal.side,
        }
        self.recorder.save_state(state)
        payload = self.market_order_payload(signal)
        try:
            response = self.client.create_market_order(payload)
        except SubmissionUnknown as exc:
            pair_state["submission"] = {
                "status": "UNKNOWN",
                "signal_key": signal.signal_key,
                "reason": str(exc),
                "unknown_at": utc_text(),
            }
            self.recorder.save_state(state)
            self.recorder.record_order(self._order_record(signal, "UNKNOWN", error_message=str(exc)))
            self.recorder.event(
                "UNKNOWN",
                pair.instrument,
                "Practice order outcome is unknown after a transport error; inspect OANDA before any retry.",
                signal_key=signal.signal_key,
            )
            return
        except OandaApiError as exc:
            pair_state["submission"] = {
                "status": "REJECTED",
                "signal_key": signal.signal_key,
                "reason": exc.detail,
                "rejected_at": utc_text(),
            }
            self.recorder.save_state(state)
            self.recorder.record_order(
                self._order_record(signal, "REJECTED", error_code=str(exc.status), error_message=exc.detail)
            )
            self.recorder.event(
                "REJECTED",
                pair.instrument,
                "OANDA practice endpoint rejected the order; no retry will occur today.",
                status=exc.status,
            )
            return

        if isinstance(response.get("orderRejectTransaction"), dict) or response.get("errorMessage"):
            error_code = str(response.get("errorCode") or "")
            error_message = str(response.get("errorMessage") or "OANDA rejected the order.")
            pair_state["submission"] = {
                "status": "REJECTED",
                "signal_key": signal.signal_key,
                "reason": error_message,
                "rejected_at": utc_text(),
            }
            self.recorder.save_state(state)
            self.recorder.record_order(
                self._order_record(signal, "REJECTED", response, error_code=error_code, error_message=error_message)
            )
            self.recorder.event("REJECTED", pair.instrument, "OANDA practice endpoint rejected the order.", error_code=error_code)
            return

        record = self._order_record(signal, "SUBMITTED", response)
        trade_id = record["trade_id"]
        order_id = record["order_id"]
        pair_state["submission"] = {
            "status": "SUBMITTED",
            "signal_key": signal.signal_key,
            "submitted_at": utc_text(),
            "side": signal.side,
            "order_id": order_id,
            "trade_id": trade_id,
            "entry_reference": signal.entry_reference,
            "stop_price": signal.stop_price,
            "target_price": signal.target_price,
        }
        self.recorder.save_state(state)
        self.recorder.record_order(record)
        if trade_id:
            self.recorder.event(
                "SUBMITTED",
                pair.instrument,
                "Practice market order filled with broker-attached stop and target.",
                order_id=order_id,
                trade_id=trade_id,
                fill_price=record["fill_price"],
            )
        else:
            self.recorder.event(
                "SUBMITTED",
                pair.instrument,
                "Practice order accepted; inspect OANDA practice account for its final order state.",
                order_id=order_id,
            )

    def _record_invalid_signal(
        self,
        pair: PairSettings,
        candle: Candle,
        pair_state: dict[str, Any],
        state: dict[str, Any],
        reason: str,
    ) -> None:
        # Fail closed for the rest of the NY day. A detected breakout whose computed
        # stop/target/bound is unusable records a single BLOCKED outcome (one per pair
        # per day) instead of raising on every poll and wedging the pair. No order is
        # ever submitted for this instrument today.
        pair_state["submission"] = {
            "status": "BLOCKED",
            "signal_key": (
                f"practice-orb-{pair.instrument}-"
                f"{candle.new_york_start.date().isoformat()}-INVALID"
            ),
            "reason": "invalid_stop_target_or_bound",
            "blocked_at": utc_text(),
        }
        self.recorder.save_state(state)
        self.recorder.event(
            "BLOCKED",
            pair.instrument,
            "Breakout detected but computed stop, target, or entry bound is invalid; no order sent.",
            reason=reason,
        )

    def reconcile_pair(self, pair: PairSettings, pair_state: dict[str, Any]) -> None:
        submission = pair_state.get("submission")
        if not isinstance(submission, dict) or submission.get("status") != "SUBMITTED":
            return
        trade_id = str(submission.get("trade_id") or "")
        if not trade_id:
            return
        if pair_state.get("reconcile_done"):
            # The trade already settled (CLOSED) on an earlier cycle. Its final snapshot
            # was recorded then; stop polling a settled trade for the rest of the day.
            return
        try:
            trade = self.client.trade(trade_id)
        except BotError as exc:
            reason = str(exc)
            # Keep retrying on later cycles in case the problem is transient, but log
            # a given failure reason only once so a missing/expired trade record does
            # not flood the terminal. This path never submits, changes, or closes an order.
            if pair_state.get("reconcile_error") != reason:
                self.recorder.event(
                    "RECONCILE",
                    pair.instrument,
                    f"Could not retrieve the practice trade status: {reason}",
                    reason=reason,
                    trade_id=trade_id,
                )
                pair_state["reconcile_error"] = reason
            return
        pair_state.pop("reconcile_error", None)
        snapshot = {
            "instrument": pair.instrument,
            "trade_id": trade_id,
            "state": str(trade.get("state") or ""),
            "open_time": str(trade.get("openTime") or ""),
            "close_time": str(trade.get("closeTime") or ""),
            "entry_price": str(trade.get("price") or ""),
            "current_units": str(trade.get("currentUnits") or ""),
            "unrealized_pl": str(trade.get("unrealizedPL") or ""),
            "realized_pl": str(trade.get("realizedPL") or ""),
        }
        previous = pair_state.get("trade_snapshot")
        if previous != snapshot:
            row = {"checked_at": utc_text(), **snapshot}
            self.recorder.record_trade_snapshot(row)
            self.recorder.event(
                "TRADE",
                pair.instrument,
                "Practice trade reconciliation updated.",
                trade_id=trade_id,
                state=snapshot["state"],
                unrealized_pl=snapshot["unrealized_pl"],
                realized_pl=snapshot["realized_pl"],
            )
            pair_state["trade_snapshot"] = snapshot
        if snapshot["state"].upper() == "CLOSED" or snapshot["close_time"]:
            # Settled trade: mark done so later cycles skip the trade lookup entirely.
            pair_state["reconcile_done"] = True

    def process_pair(self, pair: PairSettings, state: dict[str, Any], replay_today: bool) -> None:
        now_new_york = datetime.now(tz=NEW_YORK)
        session_date = now_new_york.date()
        if now_new_york.isoweekday() > 5:
            self.recorder.event("STATUS", pair.instrument, "No new practice orders on Saturday or Sunday New York time.")
            return

        pairs_state = state.setdefault("pairs", {})
        pair_state = pairs_state.get(pair.instrument)
        if not isinstance(pair_state, dict) or pair_state.get("session_date") != session_date.isoformat() or replay_today:
            pair_state = fresh_pair_state(session_date)
            pairs_state[pair.instrument] = pair_state
            if replay_today:
                self.recorder.event("STATUS", pair.instrument, "Replaying current-session candles in dry-run audit mode.")

        candles = ny_session_candles(
            self.market_data.get_completed_m15_candles(pair.instrument, self.history_count), session_date
        )
        if not candles:
            self.recorder.event("STATUS", pair.instrument, "No completed M15 candles returned for the current New York date.")
            return
        if not self.establish_range(pair, candles, session_date, pair_state):
            self.recorder.event("STATUS", pair.instrument, "Waiting for both opening-range candles to complete.")
            return

        self.reconcile_pair(pair, pair_state)
        submission = pair_state.get("submission")
        if isinstance(submission, dict):
            # One signal/order outcome per pair per NY day, including blocked,
            # rejected, and unknown outcomes. This fails closed after uncertainty.
            return

        if replay_today:
            candidates = candles
        elif pair_state.get("last_processed"):
            checkpoint = parse_iso_datetime(str(pair_state["last_processed"]))
            candidates = [candle for candle in candles if candle.start > checkpoint]
        else:
            candidates = [candles[-1]]

        for candle in candidates:
            try:
                signal_to_submit = self.signal_from_candle(pair, candle, pair_state)
            except InvalidSignalParameters as exc:
                self._record_invalid_signal(pair, candle, pair_state, state, str(exc))
            else:
                if signal_to_submit is not None:
                    self.submit_signal(pair, signal_to_submit, pair_state, state)
            pair_state["last_processed"] = candle.start.isoformat().replace("+00:00", "Z")
            if isinstance(pair_state.get("submission"), dict):
                break

        if candidates:
            self.recorder.event(
                "STATUS",
                pair.instrument,
                "Processed completed M15 candle data.",
                processed_candles=len(candidates),
                last_processed=pair_state["last_processed"],
            )

    def run_cycle(self, state: dict[str, Any], replay_today: bool) -> None:
        # Isolate pairs from one another: a failure processing one instrument must not
        # skip the remaining pairs, and state is always persisted at the end of the cycle.
        try:
            for pair in self.pairs:
                try:
                    self.process_pair(pair, state, replay_today)
                except BotError as exc:
                    self.recorder.event(
                        "ERROR",
                        pair.instrument,
                        f"Pair processing failed this cycle; other pairs continue: {exc}",
                        reason=str(exc),
                    )
                except Exception as exc:  # Defensive: never let one pair abort the whole cycle.
                    self.recorder.event(
                        "ERROR",
                        pair.instrument,
                        "Unexpected pair failure this cycle; other pairs continue: "
                        f"{exc.__class__.__name__}: {exc}",
                    )
        finally:
            self.recorder.save_state(state)

    def status(self) -> None:
        summary = self.client.account_summary()
        terminal_alert(
            "STATUS",
            "Practice account preflight succeeded "
            f"({masked_account_id(self.execution_settings.account_id)}): "
            f"NAV={summary.get('NAV', 'unknown')}, marginAvailable={summary.get('marginAvailable', 'unknown')}, "
            f"openTradeCount={summary.get('openTradeCount', 'unknown')}, "
            f"pendingOrderCount={summary.get('pendingOrderCount', 'unknown')}. "
            "No order was submitted.",
        )


def create_executor(config_path: Path, armed: bool) -> tuple[PracticeExecutor, PracticeRecorder, dict[str, Any]]:
    config = load_practice_config(config_path)
    base_dir = config_path.parent
    logging = config["logging"]
    recorder = PracticeRecorder(
        state_path=base_dir / str(logging["state_file"]),
        events_path=base_dir / str(logging["events_file"]),
        orders_path=base_dir / str(logging["orders_file"]),
        trades_path=base_dir / str(logging["trades_file"]),
    )
    token = os.getenv("OANDA_API_TOKEN", "").strip()
    settings = parse_execution_settings(config)
    base_url = require_exact_practice_url(str(config["market_data"]["base_url"]))
    market_data = OandaPracticeMarketData(base_url, token, settings.timeout_seconds)
    execution_client = OandaPracticeExecutionClient(base_url, token, settings.account_id, settings.timeout_seconds)
    executor = PracticeExecutor(config, recorder, market_data, execution_client, settings, armed)
    executor.initialize()
    return executor, recorder, recorder.load_state()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Practice-only OANDA executor for the 15-minute New York opening-range breakout strategy."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("practice_execution.toml"),
        help="Path to the separate practice-only TOML configuration file.",
    )
    parser.add_argument("--once", action="store_true", help="Run one cycle, then exit.")
    parser.add_argument(
        "--replay-today",
        action="store_true",
        help="Replay completed current-session candles in dry-run audit mode; may not submit orders.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Verify account and instrument access without processing signals or submitting orders.",
    )
    parser.add_argument(
        "--confirm-practice-orders",
        action="store_true",
        help="Explicitly arm practice-only market-order submission for this process. No live URL is accepted.",
    )
    arguments = parser.parse_args()
    if arguments.replay_today and not arguments.once:
        raise BotError("--replay-today must be used with --once.")
    if arguments.replay_today and arguments.confirm_practice_orders:
        raise BotError("--replay-today is always dry-run and may not be combined with --confirm-practice-orders.")
    if arguments.status and (arguments.replay_today or arguments.confirm_practice_orders):
        raise BotError("--status does not process signals and cannot be combined with replay or order arming.")

    config_path = arguments.config.expanduser().resolve()
    executor, recorder, state = create_executor(config_path, arguments.confirm_practice_orders)
    if arguments.status:
        executor.status()
        return 0

    poll_seconds = int(executor.config["bot"]["poll_interval_seconds"])
    if poll_seconds < 15:
        raise BotError("bot.poll_interval_seconds must be at least 15 seconds.")
    if arguments.confirm_practice_orders:
        terminal_alert(
            "WARNING",
            "Practice order submissions are armed for this process only. "
            "The exact OANDA practice URL, explicit account ID, and preflight checks are required.",
        )
    else:
        terminal_alert("NOTICE", "Dry-run mode: signals will be logged but no OANDA orders can be submitted.")

    should_stop = False

    def stop_handler(_signum: int, _frame: Any) -> None:
        nonlocal should_stop
        should_stop = True
        terminal_alert("NOTICE", "Stop requested; the executor will exit after the current cycle.")

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    while not should_stop:
        try:
            executor.run_cycle(state, arguments.replay_today)
        except BotError as exc:
            terminal_alert("ERROR", str(exc))
        except Exception as exc:  # Defensive boundary: retain logs for unexpected faults.
            terminal_alert("ERROR", f"Unexpected failure: {exc.__class__.__name__}: {exc}")
        if arguments.once or should_stop:
            break
        time.sleep(poll_seconds)

    recorder.save_state(state)
    terminal_alert("NOTICE", "Practice-only executor stopped.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BotError as exc:
        terminal_alert("ERROR", str(exc))
        raise SystemExit(2)
