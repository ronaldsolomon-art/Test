"""P0: market_order_payload turns a Signal into the OANDA order body.

A sign error here would open a position in the *wrong direction*, so the
LONG/SHORT unit sign is the single most important assertion in the suite.
"""
from __future__ import annotations

from practice_orb_executor import PracticeExecutor

from conftest import make_signal


class TestMarketOrderPayload:
    def test_long_units_are_positive(self):
        payload = PracticeExecutor.market_order_payload(make_signal("LONG", units=1000))
        assert payload["units"] == "1000"

    def test_short_units_are_negative(self):
        payload = PracticeExecutor.market_order_payload(make_signal("SHORT", units=1000))
        assert payload["units"] == "-1000"

    def test_static_order_shape(self):
        payload = PracticeExecutor.market_order_payload(make_signal("LONG"))
        assert payload["type"] == "MARKET"
        assert payload["instrument"] == "EUR_USD"
        assert payload["timeInForce"] == "FOK"
        assert payload["positionFill"] == "DEFAULT"

    def test_stop_target_and_bound_come_from_signal(self):
        signal = make_signal("LONG", stop="1.10000", target="1.10300", bound="1.10180")
        payload = PracticeExecutor.market_order_payload(signal)
        assert payload["priceBound"] == "1.10180"
        assert payload["stopLossOnFill"] == {"timeInForce": "GTC", "price": "1.10000"}
        assert payload["takeProfitOnFill"] == {"timeInForce": "GTC", "price": "1.10300"}

    def test_units_string_not_int(self):
        # OANDA expects units as a string; a raw int would be rejected by the API.
        payload = PracticeExecutor.market_order_payload(make_signal("LONG"))
        assert isinstance(payload["units"], str)
