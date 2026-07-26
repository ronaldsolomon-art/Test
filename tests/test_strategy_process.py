"""P1: establish_range + process_candle — the entry gating and lifecycle.

Key invariants pinned here:
* the range needs BOTH 09:30 and 09:45 candles;
* no NEW entry before 10:00 or after the 16:45 cutoff;
* an already-open position is still MANAGED after the cutoff (the easy
  regression called out in the docstring of process_candle);
* one trade per pair per day;
* invalid stop/target geometry opens no trade.
"""
from __future__ import annotations

import pytest

from paper_orb_bot import OpeningRangeStrategy, make_fresh_pair_state

from conftest import SESSION_DATE, ny_candle


def make_strategy(recorder, *, target_points="150"):
    pair = {
        "instrument": "EUR_USD",
        "point_size": "0.00001",
        "fixed_units": 1000,
        "display_precision": 5,
        "target_points": target_points,
    }
    bot = {"target_points": 10, "max_trades_per_day": 1, "entry_cutoff_new_york": "16:45"}
    return OpeningRangeStrategy(pair, bot, recorder)


# Range candles yield high=1.10100, low=1.10000.
RANGE_CANDLES = [
    ny_candle(9, 30, "1.10060", "1.10100", "1.10050", "1.10070"),
    ny_candle(9, 45, "1.10070", "1.10080", "1.10000", "1.10010"),
]


def established(recorder, strategy):
    pair_state = make_fresh_pair_state(SESSION_DATE)
    assert strategy.establish_range(RANGE_CANDLES, SESSION_DATE, pair_state) is True
    return pair_state


class TestEstablishRange:
    def test_sets_high_and_low(self, recorder):
        strategy = make_strategy(recorder)
        pair_state = established(recorder, strategy)
        assert pair_state["range"]["high"] == "1.10100"
        assert pair_state["range"]["low"] == "1.10000"

    def test_missing_one_range_candle_returns_false(self, recorder):
        strategy = make_strategy(recorder)
        pair_state = make_fresh_pair_state(SESSION_DATE)
        only_first = [RANGE_CANDLES[0]]
        assert strategy.establish_range(only_first, SESSION_DATE, pair_state) is False
        assert pair_state["range"] is None


class TestProcessCandle:
    def test_long_breakout_opens_trade(self, recorder):
        strategy = make_strategy(recorder)
        pair_state = established(recorder, strategy)
        strategy.process_candle(ny_candle(10, 0, "1.10100", "1.10160", "1.10090", "1.10150"), pair_state)
        trade = pair_state["trade"]
        assert trade is not None
        assert trade["side"] == "LONG"
        assert trade["stop_price"] == "1.10000"
        assert trade["target_price"] == "1.10300"
        assert "ENTRY" in recorder.event_types()

    def test_short_breakout_opens_trade(self, recorder):
        strategy = make_strategy(recorder)
        pair_state = established(recorder, strategy)
        strategy.process_candle(ny_candle(10, 0, "1.10000", "1.10010", "1.09940", "1.09950"), pair_state)
        assert pair_state["trade"]["side"] == "SHORT"
        assert pair_state["trade"]["stop_price"] == "1.10100"

    def test_no_breakout_inside_range_opens_nothing(self, recorder):
        strategy = make_strategy(recorder)
        pair_state = established(recorder, strategy)
        strategy.process_candle(ny_candle(10, 0, "1.10050", "1.10090", "1.10010", "1.10050"), pair_state)
        assert pair_state["trade"] is None

    def test_no_new_entry_before_ten(self, recorder):
        strategy = make_strategy(recorder)
        pair_state = established(recorder, strategy)
        # A breakout-sized close at 09:30 must not open a trade (entry gate).
        strategy.process_candle(ny_candle(9, 30, "1.10100", "1.10200", "1.10090", "1.10150"), pair_state)
        assert pair_state["trade"] is None

    def test_no_new_entry_after_cutoff(self, recorder):
        strategy = make_strategy(recorder)
        pair_state = established(recorder, strategy)
        strategy.process_candle(ny_candle(17, 0, "1.10100", "1.10200", "1.10090", "1.10150"), pair_state)
        assert pair_state["trade"] is None
        assert "ENTRY" not in recorder.event_types()

    def test_open_position_is_managed_after_cutoff(self, recorder):
        # THE regression guard: the cutoff gates new entries only. A position
        # already on must still be allowed to reach its target after 16:45.
        strategy = make_strategy(recorder)
        pair_state = established(recorder, strategy)
        strategy.process_candle(ny_candle(10, 0, "1.10100", "1.10160", "1.10090", "1.10150"), pair_state)
        assert pair_state["trade"]["status"] == "OPEN"
        # A post-cutoff bar reaching the target closes the trade.
        strategy.process_candle(ny_candle(17, 0, "1.10200", "1.10350", "1.10190", "1.10300"), pair_state)
        assert pair_state["trade"]["status"] == "CLOSED"
        assert pair_state["trade"]["exit_reason"] == "TARGET"
        assert len(recorder.closed_trades) == 1

    def test_one_trade_per_day(self, recorder):
        strategy = make_strategy(recorder)
        pair_state = established(recorder, strategy)
        # Open then close on the target.
        strategy.process_candle(ny_candle(10, 0, "1.10100", "1.10160", "1.10090", "1.10150"), pair_state)
        strategy.process_candle(ny_candle(10, 15, "1.10200", "1.10350", "1.10190", "1.10300"), pair_state)
        assert len(recorder.closed_trades) == 1
        # A second, later breakout must NOT open another trade.
        strategy.process_candle(ny_candle(11, 0, "1.10100", "1.10200", "1.10090", "1.10150"), pair_state)
        assert len(recorder.closed_trades) == 1

    def test_invalid_geometry_opens_no_trade(self, recorder):
        # Extreme target_points drives a SHORT target below zero -> STATUS, no trade.
        strategy = make_strategy(recorder, target_points="200000")
        pair_state = established(recorder, strategy)
        strategy.process_candle(ny_candle(10, 0, "1.10000", "1.10010", "1.09940", "1.09950"), pair_state)
        assert pair_state["trade"] is None
        assert "STATUS" in recorder.event_types()
        assert "ENTRY" not in recorder.event_types()
