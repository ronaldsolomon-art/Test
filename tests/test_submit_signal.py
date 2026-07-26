"""P0: submit_signal is the order-submission state machine.

Its job is to submit at most one order per pair per day and to *fail closed* on
any uncertainty so a duplicate real order can never be sent. These tests pin:

* dry-run never POSTs,
* a blocked preflight never POSTs,
* the SUBMISSION_STARTED marker is persisted *before* the POST,
* transport-unknown -> UNKNOWN (blocks same-day retry),
* API/embedded rejection -> REJECTED,
* success -> SUBMITTED with the broker trade id.
"""
from __future__ import annotations

import pytest

from practice_orb_executor import OandaApiError, SubmissionUnknown

from conftest import make_signal


def _state_for(pair_instrument: str) -> tuple[dict, dict]:
    pair_state: dict = {"session_date": "2025-01-06"}
    state = {"pairs": {pair_instrument: pair_state}}
    return state, pair_state


class TestDryRun:
    def test_dry_run_records_dryrun_and_never_posts(self, make_executor, pair_settings):
        executor, rec, client, _ = make_executor(armed=False)
        state, pair_state = _state_for(pair_settings.instrument)
        executor.submit_signal(pair_settings, make_signal("LONG"), pair_state, state)

        assert "create_market_order" not in client.calls
        assert pair_state["submission"]["status"] == "DRYRUN"
        assert "DRYRUN" in rec.event_types()


class TestBlockedPreflight:
    def test_blocked_preflight_never_posts(self, make_executor, pair_settings):
        executor, rec, client, _ = make_executor(armed=True)
        client.price_status = "halted"  # force a preflight block
        state, pair_state = _state_for(pair_settings.instrument)
        executor.submit_signal(pair_settings, make_signal("LONG"), pair_state, state)

        assert "create_market_order" not in client.calls
        assert pair_state["submission"]["status"] == "BLOCKED"
        assert pair_state["submission"]["reason"] == "instrument_not_tradeable"


class TestSubmissionMarkerOrdering:
    def test_started_marker_persisted_before_post(self, make_executor, pair_settings):
        # The crash-safety invariant: state shows SUBMISSION_STARTED before the
        # only POST leaves the process, so an interrupted submit blocks retries.
        executor, rec, client, timeline = make_executor(armed=True)
        state, pair_state = _state_for(pair_settings.instrument)
        executor.submit_signal(pair_settings, make_signal("LONG"), pair_state, state)

        post_index = next(i for i, e in enumerate(timeline) if e[0] == "POST")
        started_saves_before_post = [
            i
            for i, e in enumerate(timeline)
            if i < post_index
            and e[0] == "SAVE"
            and e[1].get(pair_settings.instrument) == "SUBMISSION_STARTED"
        ]
        assert started_saves_before_post, "SUBMISSION_STARTED must be saved before the POST"


class TestTransportUnknown:
    def test_transport_error_marks_unknown(self, make_executor, pair_settings):
        executor, rec, client, _ = make_executor(armed=True)
        client.order_exception = SubmissionUnknown("transport died mid-POST")
        state, pair_state = _state_for(pair_settings.instrument)
        executor.submit_signal(pair_settings, make_signal("LONG"), pair_state, state)

        assert pair_state["submission"]["status"] == "UNKNOWN"
        assert rec.orders and rec.orders[-1]["submission_status"] == "UNKNOWN"
        assert "UNKNOWN" in rec.event_types()


class TestRejection:
    def test_api_error_marks_rejected(self, make_executor, pair_settings):
        executor, rec, client, _ = make_executor(armed=True)
        client.order_exception = OandaApiError(400, "INSUFFICIENT_MARGIN")
        state, pair_state = _state_for(pair_settings.instrument)
        executor.submit_signal(pair_settings, make_signal("LONG"), pair_state, state)

        assert pair_state["submission"]["status"] == "REJECTED"
        assert rec.orders[-1]["submission_status"] == "REJECTED"
        assert rec.orders[-1]["error_code"] == "400"

    def test_embedded_reject_transaction_marks_rejected(self, make_executor, pair_settings):
        executor, rec, client, _ = make_executor(armed=True)
        client.order_response = {
            "orderCreateTransaction": {"id": "111"},
            "orderRejectTransaction": {"id": "112", "reason": "FIFO_VIOLATION"},
            "errorCode": "FIFO_VIOLATION",
            "errorMessage": "rejected by broker",
        }
        state, pair_state = _state_for(pair_settings.instrument)
        executor.submit_signal(pair_settings, make_signal("LONG"), pair_state, state)

        assert pair_state["submission"]["status"] == "REJECTED"
        assert rec.orders[-1]["error_message"] == "rejected by broker"


class TestSuccessfulSubmission:
    def test_fill_marks_submitted_with_trade_id(self, make_executor, pair_settings):
        executor, rec, client, _ = make_executor(armed=True)
        state, pair_state = _state_for(pair_settings.instrument)
        executor.submit_signal(pair_settings, make_signal("LONG"), pair_state, state)

        submission = pair_state["submission"]
        assert submission["status"] == "SUBMITTED"
        assert submission["trade_id"] == "333"
        assert submission["order_id"] == "111"
        order = rec.orders[-1]
        assert order["submission_status"] == "SUBMITTED"
        assert order["fill_price"] == "1.10150"
        assert "SUBMITTED" in rec.event_types()

    def test_short_submission_sends_negative_units(self, make_executor, pair_settings):
        executor, rec, client, timeline = make_executor(armed=True)
        state, pair_state = _state_for(pair_settings.instrument)
        executor.submit_signal(pair_settings, make_signal("SHORT"), pair_state, state)

        post = next(e for e in timeline if e[0] == "POST")
        assert post[1]["units"] == "-1000"
