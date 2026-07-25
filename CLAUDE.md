# CLAUDE.md

Guidance for AI assistants (and humans) working in this repository.

## What this project is

A **practice-only** automated order executor for the **15-minute New York
opening-range breakout (ORB)** forex strategy, targeting OANDA's v20 REST API.

The entire program is deliberately hard-wired to OANDA's HTTPS **practice**
endpoint (`https://api-fxpractice.oanda.com`) and has **no live-account mode**.
It exists to run controlled experiments against a practice account — never to
trade real money. Safety, auditability, and "fail closed" behavior are valued
over cleverness or throughput. Preserve that posture in every change.

## Repository layout

This branch currently contains only three tracked files:

| Path | Purpose |
|------|---------|
| `practice_orb_executor.py` | The entire executor program (~1050 lines, single module). |
| `practice_execution.toml`  | Practice-only configuration. Contains **no** token and **no** account ID. |
| `.gitignore`               | Ignores `__pycache__/`, `*.pyc`, and the runtime `practice_data/` directory. |

### Important missing dependency

`practice_orb_executor.py` imports shared, read-only helpers from a peer module
**`paper_orb_bot.py`**:

```python
from paper_orb_bot import (
    BotError, Candle, NEW_YORK, UTC,
    RANGE_START, RANGE_END, ENTRY_START,
    OandaPracticeMarketData,
    append_jsonl, decimal_text, parse_iso_datetime,
    terminal_alert, write_json_atomic,
)
```

**`paper_orb_bot.py` is not present in this repository/branch.** The executor
will not import or run without it (`ModuleNotFoundError: No module named
'paper_orb_bot'`). If you are asked to run or extend the program, first confirm
whether `paper_orb_bot.py` is expected to be added here or supplied alongside.
Do not stub or reimplement its symbols without checking with the user — those
helpers (candle parsing, the NY session time constants, atomic JSON writes,
terminal alerts) define shared behavior the executor relies on.

Constants that live in `paper_orb_bot.py` and govern the strategy timing:
`RANGE_START`/`RANGE_END` (the 09:30 and 09:45 New York opening-range candles),
`ENTRY_START` (earliest breakout entry), and the `NEW_YORK`/`UTC` timezones.

## Runtime requirements

- **Python 3.11+** — required for the standard-library `tomllib` (TOML parser).
- **Standard library only.** No third-party packages, no `requirements.txt`, no
  package manifest. HTTP is done with `urllib.request`; timezones with
  `zoneinfo`. Keep it dependency-free unless the user explicitly asks otherwise.

### Required environment variables

Secrets are **never** stored in files. They are read from the shell:

- `OANDA_API_TOKEN` — a practice-account API token.
- `OANDA_PRACTICE_ACCOUNT_ID` — your practice account ID (the env var name is
  configurable via `execution.account_id_env`).

## How to run

```bash
# Verify account/instrument access only — never submits an order:
python3 practice_orb_executor.py --status

# Dry-run loop (logs signals, submits nothing) — this is the default:
python3 practice_orb_executor.py

# Replay the current session's completed candles once, dry-run audit only:
python3 practice_orb_executor.py --once --replay-today

# ARM real practice-order submission (still practice-only URL):
python3 practice_orb_executor.py --confirm-practice-orders
```

CLI flags (`main()` in `practice_orb_executor.py`):

- `--config PATH` — TOML config path (default `practice_execution.toml`).
- `--once` — run a single cycle then exit.
- `--replay-today` — replay today's candles in dry-run audit mode. Must be used
  with `--once`; cannot be combined with `--confirm-practice-orders`.
- `--status` — preflight account/instrument access, submit nothing. Cannot be
  combined with replay or arming.
- `--confirm-practice-orders` — the **arming flag**. Without it, no order can be
  submitted regardless of configuration; the program stays in dry-run mode.

## Safety model — do not weaken these guarantees

These invariants are the reason the program exists. Treat them as load-bearing:

1. **Exact practice URL only.** `require_exact_practice_url()` rejects any
   `market_data.base_url` other than `https://api-fxpractice.oanda.com`. There
   is no live mode and none should be added.
2. **`execution.environment` must equal `"practice_only"`**, or the config load
   fails.
3. **Explicit arming.** Order submission requires `--confirm-practice-orders`
   every run. Dry-run is the default.
4. **Single POST.** `create_market_order()` is the *only* mutating request in
   the program. Everything else is a GET. Preserve this — new write endpoints
   (funding, closes, config changes, leverage) are out of scope.
5. **Secrets stay out of files and logs.** The token is never printed; account
   IDs are shown via `masked_account_id()`.
6. **One signal/order outcome per pair per New York day** — including BLOCKED,
   REJECTED, and UNKNOWN outcomes. The bot **fails closed** after any
   uncertainty rather than retrying.
7. **Crash-safe submission.** A `SUBMISSION_STARTED` marker is persisted to
   state *before* the POST. A transport error mid-POST raises `SubmissionUnknown`
   and blocks same-day retries so an order is never accidentally duplicated.
8. **Preflight guards** (`_preflight`) check margin floor, max total open/pending
   trades, instrument tradeable status, and no existing open/pending order for
   the instrument before arming a submission.

## Strategy mechanics (for correct edits)

- Opening range = high/low across the **09:30 and 09:45** New York M15 candles
  (`establish_range`). Both must have completed before any signal.
- Entry window: a breakout candle whose New York start time is between
  `ENTRY_START` and `bot.entry_cutoff_new_york` (config default `16:45`).
- **LONG** when a candle closes above the range high; **SHORT** below the range
  low (`signal_from_candle`).
- **Target** = fixed `bot.target_points` × the pair's `point_size` from the
  breakout close. **Stop** = the opposite range bound. Both attach to the fill
  via `stopLossOnFill` / `takeProfitOnFill`.
- Orders are **MARKET / FOK** with a `priceBound` derived from
  `max_entry_deviation_points`, so a fill too far from the signal close is
  cancelled rather than chased.
- No orders on Saturday/Sunday New York time.
- Reconciliation (`reconcile_pair`) polls the resulting trade and records
  snapshots until it settles (CLOSED); it never modifies or closes trades.

## Configuration (`practice_execution.toml`)

Required sections: `[bot]`, `[market_data]`, `[execution]`, `[logging]`, and at
least one `[[pairs]]`. Notable keys:

- `[bot]`: `target_points`, `poll_interval_seconds` (min 15),
  `history_candle_count` (min 8), `entry_cutoff_new_york`.
- `[market_data]`: `base_url` (must be the exact practice URL), `timeout_seconds`.
- `[execution]`: `environment = "practice_only"`, `account_id_env`,
  `max_total_open_trades`, `max_units_per_order`, `min_margin_available`
  (may be 0, never negative), `max_entry_deviation_points`.
- `[[pairs]]`: `instrument`, `display_precision`, `point_size`, `fixed_units`.
  On startup, `initialize()` cross-checks each pair against OANDA instrument
  metadata (display precision, integer units only, min/max order size) and
  refuses to arm if the config disagrees.

## Runtime data (gitignored — never commit)

Written under `practice_data/` (paths from `[logging]`):

- `state.json` — durable per-pair session state (atomic writes).
- `events.jsonl` — append-only structured event log.
- `practice_orders.csv` — sanitized order audit trail.
- `practice_trade_snapshots.csv` — trade reconciliation snapshots.

## Code conventions

- **Prices and money use `decimal.Decimal`**, formatted for output via
  `decimal_text(...)`. Never use floats for prices or thresholds.
- **Dataclasses are frozen** (`PairSettings`, `ExecutionSettings`, `Signal`) —
  treat signal/config values as immutable.
- **Timestamps** are UTC ISO-8601 with a `Z` suffix (`utc_text()`); strategy
  timing decisions are made in New York local time.
- **Errors** derive from `BotError`; the specialized ones (`OandaApiError`,
  `SubmissionUnknown`, `InvalidSignalParameters`) carry safety meaning — don't
  collapse them into generic exceptions.
- **Fail closed:** an invalid computed stop/target/bound records a single
  BLOCKED outcome for the pair for the day rather than raising every poll.
- **Pair isolation:** a failure processing one instrument must never abort the
  cycle for the others (`run_cycle`); state is always saved in a `finally`.
- Match the existing style: type hints throughout, small guarded methods,
  explanatory comments only where a safety decision needs justifying.

## Testing / verification

There is **no test suite** in this repository. To sanity-check changes:

```bash
python3 -m py_compile practice_orb_executor.py   # syntax check
```

A full `import` or run requires `paper_orb_bot.py` (see above). Because live
runs touch a real OANDA practice account, prefer `--status` and
`--once --replay-today` for verification, and never add automated tests that
POST orders.

## Git workflow

- Development for the current documentation task happens on branch
  `claude/claude-md-docs-27ua0e`.
- The executor code originates on `claude/evaluate-optimize-9ha3v2`.
- Commit with clear, descriptive messages. Never commit anything under
  `practice_data/`, and never commit tokens or account IDs.
- Do not open a pull request unless explicitly asked.
