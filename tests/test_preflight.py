"""P0: _preflight is the fail-closed gate in front of every armed submission.

Each risk condition must independently block, and a healthy account must pass.
"""
from __future__ import annotations

import pytest

from paper_orb_bot import BotError


@pytest.fixture
def wired(make_executor, pair_settings):
    executor, rec, client, _ = make_executor(armed=True)
    return executor, client, pair_settings


class TestPreflight:
    def test_healthy_account_passes(self, wired):
        executor, client, pair = wired
        allowed, reason = executor._preflight(pair)
        assert allowed is True
        assert reason == "ok"

    def test_margin_below_minimum_blocks(self, wired):
        executor, client, pair = wired
        client.summary["marginAvailable"] = "-0.01"  # below the configured floor of 0
        allowed, reason = executor._preflight(pair)
        assert not allowed
        assert reason == "margin_available_below_configured_minimum"

    def test_max_open_plus_pending_blocks(self, wired):
        executor, client, pair = wired
        client.summary["openTradeCount"] = 1
        client.summary["pendingOrderCount"] = 1  # 1 + 1 >= max_total_open_trades (2)
        allowed, reason = executor._preflight(pair)
        assert not allowed
        assert reason == "maximum_total_open_or_pending_trades_reached"

    def test_instrument_not_tradeable_blocks(self, wired):
        executor, client, pair = wired
        client.price_status = "halted"
        allowed, reason = executor._preflight(pair)
        assert not allowed
        assert reason == "instrument_not_tradeable"

    def test_existing_open_trade_blocks(self, wired):
        executor, client, pair = wired
        client.open_trades_result = [{"id": "999"}]
        allowed, reason = executor._preflight(pair)
        assert not allowed
        assert reason == "existing_open_trade_for_instrument"

    def test_existing_pending_order_blocks(self, wired):
        executor, client, pair = wired
        client.pending_orders_result = [{"id": "888"}]
        allowed, reason = executor._preflight(pair)
        assert not allowed
        assert reason == "existing_pending_order_for_instrument"

    def test_missing_risk_field_raises(self, wired):
        executor, client, pair = wired
        client.summary = {"openTradeCount": 0, "pendingOrderCount": 0}  # no marginAvailable
        with pytest.raises(BotError):
            executor._preflight(pair)

    def test_margin_gate_checked_before_price(self, wired):
        # A blocked account must short-circuit before any pricing call is made.
        executor, client, pair = wired
        client.summary["marginAvailable"] = "-1"
        executor._preflight(pair)
        assert "price" not in client.calls
