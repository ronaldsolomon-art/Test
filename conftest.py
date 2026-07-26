"""Shared pytest fixtures and test doubles for the ORB bot suite.

Living at the repository root, this conftest guarantees the top-level modules
(`paper_orb_bot`, `practice_orb_executor`, ...) are importable from the tests
and provides:

* candle builders that place a bar at a given New York wall-clock time,
* an in-memory recorder that captures events/orders/state saves,
* a configurable fake OANDA execution client (no network),
* ready-made pair / execution / config fixtures, and
* a `make_executor` factory that wires a `PracticeExecutor` from fakes.

Nothing here touches the network or the filesystem.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from paper_orb_bot import Candle, NEW_YORK, UTC
from practice_orb_executor import (
    ExecutionSettings,
    PairSettings,
    PracticeExecutor,
    Signal,
)

# A default New York trading day used across the strategy tests (a Monday).
SESSION_DATE = date(2025, 1, 6)


# --------------------------------------------------------------------------- #
# Candle builders
# --------------------------------------------------------------------------- #
def ny_candle(
    hour: int,
    minute: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    day: date = SESSION_DATE,
) -> Candle:
    """A Candle whose start is `hour:minute` New York local time on `day`.

    Prices are passed as strings so they become exact Decimals (matching how
    the production code parses broker data).
    """
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK)
    return Candle(
        start=start,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def utc_candle(iso_utc: str, open_: str, high: str, low: str, close: str) -> Candle:
    """A Candle whose start is an explicit UTC instant (for DST assertions)."""
    start = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(UTC)
    return Candle(
        start=start,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


@pytest.fixture
def make_candle():
    return ny_candle


# --------------------------------------------------------------------------- #
# Recorder double
# --------------------------------------------------------------------------- #
class FakeRecorder:
    """Captures everything the production recorders would persist, in memory.

    `timeline` is a shared, ordered log of side effects. `save_state` records
    the single pair's submission status at the moment of each save, which lets
    a test assert the SUBMISSION_STARTED marker was persisted *before* the POST
    (the fake client appends its own POST marker to the same timeline).
    """

    def __init__(self, timeline: list[Any] | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.trade_snapshots: list[dict[str, Any]] = []
        self.closed_trades: list[Any] = []
        self.saves: list[dict[str, Any]] = []
        self.timeline: list[Any] = timeline if timeline is not None else []

    # practice executor + paper bot recorder API
    def event(self, event_type: str, instrument: str, message: str, **details: Any) -> None:
        self.events.append(
            {"type": event_type, "instrument": instrument, "message": message, "details": details}
        )

    def save_state(self, state: dict[str, Any]) -> None:
        # Snapshot the (single) pair's submission status for ordering assertions.
        pairs = state.get("pairs", {}) if isinstance(state, dict) else {}
        statuses = {
            inst: (ps.get("submission") or {}).get("status")
            for inst, ps in pairs.items()
            if isinstance(ps, dict)
        }
        self.saves.append(statuses)
        self.timeline.append(("SAVE", statuses))

    def record_order(self, row: dict[str, Any]) -> None:
        self.orders.append(row)
        self.timeline.append(("ORDER", row.get("submission_status")))

    def record_trade_snapshot(self, row: dict[str, Any]) -> None:
        self.trade_snapshots.append(row)

    def record_closed_trade(self, trade: Any) -> None:
        self.closed_trades.append(trade)

    # convenience accessors
    def event_types(self) -> list[str]:
        return [e["type"] for e in self.events]

    def last_event(self, event_type: str) -> dict[str, Any] | None:
        for event in reversed(self.events):
            if event["type"] == event_type:
                return event
        return None


@pytest.fixture
def recorder():
    return FakeRecorder()


# --------------------------------------------------------------------------- #
# Fake OANDA execution client
# --------------------------------------------------------------------------- #
class FakeExecClient:
    """A stand-in for OandaPracticeExecutionClient with no network.

    Every read method returns a value from an overridable attribute; the sole
    POST (`create_market_order`) returns `order_response` or raises
    `order_exception`. All calls are appended to `calls` (and `timeline`).
    """

    def __init__(self, timeline: list[Any] | None = None) -> None:
        self.timeline: list[Any] = timeline if timeline is not None else []
        self.calls: list[str] = []
        # Preflight-shaping knobs (healthy defaults that allow a submission).
        self.summary: dict[str, Any] = {
            "marginAvailable": "10000",
            "openTradeCount": 0,
            "pendingOrderCount": 0,
        }
        self.price_status = "tradeable"
        self.open_trades_result: list[dict[str, Any]] = []
        self.pending_orders_result: list[dict[str, Any]] = []
        # POST behaviour.
        self.order_response: dict[str, Any] = {
            "orderCreateTransaction": {"id": "111", "requestID": "req-1"},
            "orderFillTransaction": {
                "id": "222",
                "orderID": "111",
                "price": "1.10150",
                "requestID": "req-1",
                "tradeOpened": {"tradeID": "333"},
            },
        }
        self.order_exception: Exception | None = None
        self.trade_result: dict[str, Any] | None = None
        self.trade_exception: Exception | None = None

    def account_summary(self) -> dict[str, Any]:
        self.calls.append("account_summary")
        return self.summary

    def price(self, instrument: str) -> dict[str, Any]:
        self.calls.append("price")
        return {"status": self.price_status}

    def open_trades(self, instrument: str) -> list[dict[str, Any]]:
        self.calls.append("open_trades")
        return self.open_trades_result

    def pending_orders(self, instrument: str) -> list[dict[str, Any]]:
        self.calls.append("pending_orders")
        return self.pending_orders_result

    def create_market_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_market_order")
        self.timeline.append(("POST", payload))
        if self.order_exception is not None:
            raise self.order_exception
        return self.order_response

    def trade(self, trade_id: str) -> dict[str, Any]:
        self.calls.append("trade")
        if self.trade_exception is not None:
            raise self.trade_exception
        return self.trade_result or {}


# --------------------------------------------------------------------------- #
# Settings / config fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def pair_settings() -> PairSettings:
    return PairSettings(
        instrument="EUR_USD",
        display_precision=5,
        point_size=Decimal("0.00001"),
        units=1000,
        target_points=Decimal("150"),
    )


@pytest.fixture
def execution_settings() -> ExecutionSettings:
    return ExecutionSettings(
        account_id="001-002-1234567-003",
        timeout_seconds=15,
        max_total_open_trades=2,
        max_units_per_order=1000,
        min_margin_available=Decimal("0"),
        max_entry_deviation_points=Decimal("30"),
    )


@pytest.fixture
def exec_config() -> dict[str, Any]:
    """Minimal in-memory config accepted by PracticeExecutor.__init__."""
    return {
        "bot": {
            "target_points": 10,
            "entry_cutoff_new_york": "16:45",
            "history_candle_count": 96,
        },
        "pairs": [
            {
                "instrument": "EUR_USD",
                "display_precision": 5,
                "point_size": "0.00001",
                "fixed_units": 1000,
                "target_points": 150,
            }
        ],
    }


def make_signal(
    side: str = "LONG",
    *,
    instrument: str = "EUR_USD",
    entry: str = "1.10150",
    stop: str = "1.10000",
    target: str = "1.10300",
    bound: str = "1.10180",
    units: int = 1000,
) -> Signal:
    return Signal(
        instrument=instrument,
        side=side,
        candle_start="2025-01-06T15:00:00Z",
        entry_reference=entry,
        stop_price=stop,
        target_price=target,
        units=units,
        price_bound=bound,
        signal_key=f"practice-orb-{instrument}-2025-01-06-{side}-2025-01-06T15:00:00Z",
    )


@pytest.fixture
def signal_factory():
    return make_signal


@pytest.fixture
def make_executor(exec_config, execution_settings):
    """Factory: build a PracticeExecutor wired to fakes that share a timeline."""

    def _make(armed: bool = True):
        timeline: list[Any] = []
        rec = FakeRecorder(timeline)
        client = FakeExecClient(timeline)
        executor = PracticeExecutor(
            config=exec_config,
            recorder=rec,
            market_data=object(),  # unused on the submit path
            execution_client=client,
            execution_settings=execution_settings,
            armed=armed,
        )
        return executor, rec, client, timeline

    return _make
