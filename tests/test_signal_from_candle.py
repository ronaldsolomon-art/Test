"""P0: signal_from_candle derives a Signal (or refuses) from one candle.

Covers the entry window gating, breakout direction, stop/target/bound geometry,
and the InvalidSignalParameters fail-closed path (which _record_invalid_signal
turns into a single BLOCKED-for-the-day outcome).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from practice_orb_executor import InvalidSignalParameters, PairSettings

from conftest import ny_candle

RANGE = {"high": "1.10100", "low": "1.10000", "established_at": "2025-01-06T14:45:00Z"}


@pytest.fixture
def executor(make_executor):
    ex, _rec, _client, _tl = make_executor(armed=True)
    return ex


def pair_state_with_range() -> dict:
    return {"session_date": "2025-01-06", "range": dict(RANGE)}


class TestEntryWindowGating:
    def test_before_entry_start_returns_none(self, executor, pair_settings):
        candle = ny_candle(9, 45, "1.101", "1.102", "1.100", "1.10150")
        assert executor.signal_from_candle(pair_settings, candle, pair_state_with_range()) is None

    def test_after_cutoff_returns_none(self, executor, pair_settings):
        candle = ny_candle(17, 0, "1.101", "1.104", "1.100", "1.10300")
        assert executor.signal_from_candle(pair_settings, candle, pair_state_with_range()) is None

    def test_no_range_returns_none(self, executor, pair_settings):
        candle = ny_candle(10, 0, "1.101", "1.104", "1.100", "1.10300")
        assert executor.signal_from_candle(pair_settings, candle, {"range": None}) is None

    def test_inside_range_returns_none(self, executor, pair_settings):
        candle = ny_candle(10, 0, "1.100", "1.101", "1.100", "1.10050")
        assert executor.signal_from_candle(pair_settings, candle, pair_state_with_range()) is None


class TestBreakoutGeometry:
    def test_long_breakout(self, executor, pair_settings):
        candle = ny_candle(10, 0, "1.101", "1.102", "1.100", "1.10150")
        signal = executor.signal_from_candle(pair_settings, candle, pair_state_with_range())
        assert signal is not None
        assert signal.side == "LONG"
        assert signal.stop_price == "1.10000"          # opposite range boundary
        assert signal.target_price == "1.10300"        # close + 150 * 0.00001
        assert signal.price_bound == "1.10180"         # close + 30 * 0.00001
        assert signal.units == 1000

    def test_short_breakout(self, executor, pair_settings):
        candle = ny_candle(10, 0, "1.100", "1.100", "1.099", "1.09950")
        signal = executor.signal_from_candle(pair_settings, candle, pair_state_with_range())
        assert signal is not None
        assert signal.side == "SHORT"
        assert signal.stop_price == "1.10100"          # range high
        assert signal.target_price == "1.09800"        # close - 150 * 0.00001
        assert signal.price_bound == "1.09920"         # close - 30 * 0.00001

    def test_signal_key_is_stable_and_scoped(self, executor, pair_settings):
        candle = ny_candle(10, 0, "1.101", "1.102", "1.100", "1.10150")
        signal = executor.signal_from_candle(pair_settings, candle, pair_state_with_range())
        assert signal.signal_key.startswith("practice-orb-EUR_USD-2025-01-06-LONG-")


class TestInvalidGeometry:
    def test_non_positive_target_raises(self, executor):
        # An extreme target_points drives a SHORT target below zero -> refuse.
        pair = PairSettings(
            instrument="EUR_USD",
            display_precision=5,
            point_size=Decimal("0.00001"),
            units=1000,
            target_points=Decimal("200000"),  # 200000 * 0.00001 = 2.0 > price
        )
        candle = ny_candle(10, 0, "1.100", "1.100", "1.089", "1.09000")  # SHORT breakout
        with pytest.raises(InvalidSignalParameters):
            executor.signal_from_candle(pair, candle, pair_state_with_range())


class TestRecordInvalidSignal:
    def test_records_single_blocked_outcome(self, executor, pair_settings):
        candle = ny_candle(10, 0, "1.100", "1.100", "1.089", "1.09000")
        pair_state = pair_state_with_range()
        state = {"pairs": {pair_settings.instrument: pair_state}}
        executor._record_invalid_signal(pair_settings, candle, pair_state, state, "bad geometry")

        assert pair_state["submission"]["status"] == "BLOCKED"
        assert pair_state["submission"]["reason"] == "invalid_stop_target_or_bound"
        assert pair_state["submission"]["signal_key"].endswith("-INVALID")
