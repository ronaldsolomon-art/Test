"""P1: Candle.from_oanda (data boundary) and new_york_start (DST correctness).

The whole strategy keys on New York wall-clock times (09:30/09:45/10:00/16:45),
so the UTC->NY conversion must land on the right local time in both EST and EDT.
"""
from __future__ import annotations

from datetime import time as clock_time
from decimal import Decimal

import pytest

from paper_orb_bot import BotError, Candle, RANGE_START

from conftest import utc_candle


class TestFromOanda:
    def test_parses_valid_record(self):
        candle = Candle.from_oanda(
            {"time": "2025-01-06T14:30:00.000000000Z",
             "mid": {"o": "1.10000", "h": "1.10100", "l": "1.09900", "c": "1.10050"}}
        )
        assert candle.open == Decimal("1.10000")
        assert candle.high == Decimal("1.10100")
        assert candle.close == Decimal("1.10050")

    @pytest.mark.parametrize("record", [
        {"time": "2025-01-06T14:30:00Z"},                               # no mid
        {"mid": {"o": "1", "h": "1", "l": "1", "c": "1"}},              # no time
        {"time": "2025-01-06T14:30:00Z", "mid": {"o": "1", "h": "1"}},  # missing l/c
        {"time": "not-a-time", "mid": {"o": "1", "h": "1", "l": "1", "c": "1"}},
        {"time": "2025-01-06T14:30:00Z", "mid": {"o": "x", "h": "1", "l": "1", "c": "1"}},
    ])
    def test_malformed_record_raises_boterror(self, record):
        with pytest.raises(BotError):
            Candle.from_oanda(record)


class TestNewYorkStartDst:
    def test_winter_est_maps_to_0930(self):
        # 14:30 UTC in January is 09:30 EST (UTC-5).
        candle = utc_candle("2025-01-06T14:30:00Z", "1.1", "1.1", "1.1", "1.1")
        assert candle.new_york_start.time() == clock_time(9, 30)
        assert candle.new_york_start.time() == RANGE_START
        assert candle.new_york_start.utcoffset().total_seconds() == -5 * 3600

    def test_summer_edt_maps_to_0930(self):
        # 13:30 UTC in July is 09:30 EDT (UTC-4).
        candle = utc_candle("2025-07-01T13:30:00Z", "1.1", "1.1", "1.1", "1.1")
        assert candle.new_york_start.time() == clock_time(9, 30)
        assert candle.new_york_start.utcoffset().total_seconds() == -4 * 3600

    def test_same_utc_hour_differs_by_season(self):
        # The identical 14:30 UTC instant is 09:30 in winter but 10:30 in summer;
        # this is exactly why the strategy must convert to NY local, not use UTC.
        winter = utc_candle("2025-01-06T14:30:00Z", "1.1", "1.1", "1.1", "1.1")
        summer = utc_candle("2025-07-01T14:30:00Z", "1.1", "1.1", "1.1", "1.1")
        assert winter.new_york_start.time() == clock_time(9, 30)
        assert summer.new_york_start.time() == clock_time(10, 30)
