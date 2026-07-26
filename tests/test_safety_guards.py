"""P0: the guards that keep the executor pointed at the practice endpoint and
that never leak the account ID."""
from __future__ import annotations

import pytest

from paper_orb_bot import BotError
from practice_orb_executor import (
    PRACTICE_BASE_URL,
    masked_account_id,
    require_exact_practice_url,
)


class TestRequireExactPracticeUrl:
    def test_accepts_exact_practice_url(self):
        assert require_exact_practice_url(PRACTICE_BASE_URL) == PRACTICE_BASE_URL

    def test_strips_trailing_slash(self):
        assert require_exact_practice_url(PRACTICE_BASE_URL + "/") == PRACTICE_BASE_URL

    def test_rejects_live_trading_url(self):
        with pytest.raises(BotError):
            require_exact_practice_url("https://api-fxtrade.oanda.com")

    def test_rejects_lookalike_host(self):
        with pytest.raises(BotError):
            require_exact_practice_url("https://api-fxpractice.oanda.com.evil.example")

    def test_rejects_empty_string(self):
        with pytest.raises(BotError):
            require_exact_practice_url("")


class TestMaskedAccountId:
    def test_masks_all_but_last_four(self):
        assert masked_account_id("001-002-1234567-003") == "***-003"

    @pytest.mark.parametrize("value", ["", "a", "1234", "abcd"])
    def test_short_ids_fully_masked(self, value):
        # Never reveal any character of a short account ID.
        assert masked_account_id(value) == "****"

    def test_five_char_id_reveals_only_last_four(self):
        assert masked_account_id("12345") == "***2345"

    def test_full_id_is_never_present_in_mask(self):
        account = "001-002-1234567-003"
        assert account not in masked_account_id(account)
