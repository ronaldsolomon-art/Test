"""P1: OpeningRangeStrategy._evaluate_exit and its 'stop-first' tie-break.

When a single M15 bar spans BOTH the stop and the target, the strategy
deliberately assumes the stop was hit first (the conservative choice). These
tests lock that behaviour and the boundary-touch (<=/>=) semantics for both
sides; an off-by-one here changes every backtest number.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from paper_orb_bot import OpeningRangeStrategy, PaperTrade

from conftest import ny_candle


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


def open_long() -> PaperTrade:
    return PaperTrade(
        instrument="EUR_USD", side="LONG", units=1000,
        entry_time="2025-01-06T15:00:00Z", entry_price="1.10150",
        stop_price="1.10000", target_price="1.10300",
        range_high="1.10100", range_low="1.10000",
    )


def open_short() -> PaperTrade:
    return PaperTrade(
        instrument="EUR_USD", side="SHORT", units=1000,
        entry_time="2025-01-06T15:00:00Z", entry_price="1.10000",
        stop_price="1.10100", target_price="1.09800",
        range_high="1.10100", range_low="1.10000",
    )


class TestLongExit:
    def test_stop_first_when_bar_spans_both(self, recorder):
        strategy = make_strategy(recorder)
        trade = open_long()
        # low <= stop AND high >= target in one bar -> STOP wins.
        strategy._evaluate_exit(ny_candle(10, 15, "1.102", "1.10350", "1.09900", "1.101"), trade)
        assert trade.status == "CLOSED"
        assert trade.exit_reason == "STOP"
        assert trade.exit_price == "1.10000"

    def test_target_only(self, recorder):
        strategy = make_strategy(recorder)
        trade = open_long()
        strategy._evaluate_exit(ny_candle(10, 15, "1.102", "1.10350", "1.10100", "1.103"), trade)
        assert trade.exit_reason == "TARGET"
        assert trade.exit_price == "1.10300"
        assert trade.pnl_quote_currency == "1.50000"  # (1.10300-1.10150)*1000

    def test_stop_only(self, recorder):
        strategy = make_strategy(recorder)
        trade = open_long()
        strategy._evaluate_exit(ny_candle(10, 15, "1.101", "1.10200", "1.09900", "1.100"), trade)
        assert trade.exit_reason == "STOP"

    def test_no_touch_leaves_trade_open(self, recorder):
        strategy = make_strategy(recorder)
        trade = open_long()
        strategy._evaluate_exit(ny_candle(10, 15, "1.101", "1.10200", "1.10050", "1.101"), trade)
        assert trade.status == "OPEN"
        assert recorder.closed_trades == []

    def test_stop_touch_is_inclusive(self, recorder):
        strategy = make_strategy(recorder)
        trade = open_long()
        # low exactly equals the stop -> counts as a stop-out.
        strategy._evaluate_exit(ny_candle(10, 15, "1.101", "1.10200", "1.10000", "1.101"), trade)
        assert trade.exit_reason == "STOP"

    def test_target_touch_is_inclusive(self, recorder):
        strategy = make_strategy(recorder)
        trade = open_long()
        # high exactly equals the target, low stays above stop -> target.
        strategy._evaluate_exit(ny_candle(10, 15, "1.102", "1.10300", "1.10100", "1.103"), trade)
        assert trade.exit_reason == "TARGET"


class TestShortExit:
    def test_stop_first_when_bar_spans_both(self, recorder):
        strategy = make_strategy(recorder)
        trade = open_short()
        # high >= stop AND low <= target in one bar -> STOP wins.
        strategy._evaluate_exit(ny_candle(10, 15, "1.100", "1.10150", "1.09750", "1.100"), trade)
        assert trade.exit_reason == "STOP"
        assert trade.exit_price == "1.10100"

    def test_target_only(self, recorder):
        strategy = make_strategy(recorder)
        trade = open_short()
        strategy._evaluate_exit(ny_candle(10, 15, "1.100", "1.10050", "1.09750", "1.098"), trade)
        assert trade.exit_reason == "TARGET"
        assert trade.exit_price == "1.09800"
        assert trade.pnl_quote_currency == "2.00000"  # (1.10000-1.09800)*1000

    def test_records_closed_trade_once(self, recorder):
        strategy = make_strategy(recorder)
        trade = open_short()
        strategy._evaluate_exit(ny_candle(10, 15, "1.100", "1.10050", "1.09750", "1.098"), trade)
        assert len(recorder.closed_trades) == 1
        assert "EXIT" in recorder.event_types()
