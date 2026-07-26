# Tests

First milestone of automated coverage for the ORB bot, focused on the
highest-risk logic: the practice-order path and the shipped strategy state
machine. All tests are pure and offline — no network, no filesystem, no live
account. Fakes and fixtures live in the repo-root `conftest.py`.

## Running

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

## Layout

| File | Priority | What it covers |
|------|----------|----------------|
| `test_safety_guards.py` | P0 | `require_exact_practice_url`, `masked_account_id` (never point live, never leak the account ID) |
| `test_order_payload.py` | P0 | `market_order_payload` — LONG/SHORT unit sign, stop/target/bound, order shape |
| `test_preflight.py` | P0 | `_preflight` fail-closed risk gates (margin, max trades, tradeable, existing trade/order) |
| `test_submit_signal.py` | P0 | submit state machine — dry-run/blocked never POST, SUBMISSION_STARTED before POST, UNKNOWN/REJECTED/SUBMITTED |
| `test_signal_from_candle.py` | P0 | entry-window gating, breakout geometry, `InvalidSignalParameters` → single BLOCKED |
| `test_strategy_exit.py` | P1 | `_evaluate_exit` stop-first tie-break + inclusive touch, both sides |
| `test_strategy_process.py` | P1 | `establish_range`, entry gating, manage-after-cutoff, one-trade-per-day, invalid geometry |
| `test_candle.py` | P1 | `Candle.from_oanda` data boundary, `new_york_start` DST correctness |

## Not yet covered (next milestones)

Analysis tools (`backtest`/`sweep`/`equity`) and their cross-implementation
equivalence, config loaders, and `reconcile_pair`. See the coverage analysis for
the full P2 list.
