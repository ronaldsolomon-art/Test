# Install & run

## Requirements

Every program here is **100% Python standard library** — there are no third-party
packages to install and no `requirements.txt`.

| Requirement | Why | Notes |
|-------------|-----|-------|
| **Python 3.11+** | `tomllib` (TOML config) is built in only from 3.11 | 3.11 / 3.12 / 3.13 all work |
| **git** | to clone the repo | any recent version |
| **`tzdata`** (Windows only) | `zoneinfo` needs the IANA timezone database, which Windows does not ship | `pip install tzdata` — the only possible install, Windows only |

For **backtesting** you need nothing beyond the above plus a candle CSV. For **running
the bots against OANDA** you also need a free OANDA *practice* account and API token,
supplied via environment variables (never stored in a file).

## Install

```bash
# 1. Clone the branch
git clone -b claude/evaluate-optimize-9ha3v2 https://github.com/ronaldsolomon-art/Test.git
cd Test

# 2. Confirm Python is 3.11+
python3 --version

# 3. (Windows only) install the timezone database
pip install tzdata
```

Optional — isolate with a virtual environment (installs nothing, just isolates Python):

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

## Backtesting (no account needed — just a candle CSV)

CSV columns: `time,open,high,low,close` with `time` in ISO-8601 UTC
(e.g. `2026-01-05T14:30:00Z`). One file per instrument. A `JPYUSD` source must be
inverted (`price -> 1/price`, swapping high/low) to trade as `USD_JPY`.

```bash
python3 backtest.py    --csv EURUSD_15m.csv --instrument EUR_USD    # win rate + expectancy
python3 sweep.py       --csv EURUSD_15m.csv --instrument EUR_USD    # target x stop-cap sweep + OOS
python3 walkforward.py --csv EURUSD_15m.csv --instrument EUR_USD    # rolling walk-forward validation
python3 equity.py      --csv EURUSD_15m.csv --start 1000 --units 10000   # account equity curve
python3 broker.py      --csv EURUSD_15m.csv --sweep-spread          # broker-accurate cost check
```

Use `--help` on any of them for the full option list.

## Running the bots (need an OANDA practice account + token)

Set your own credentials in the environment — the programs read them from there and
never from disk, and never print the token:

```bash
export OANDA_API_TOKEN="your-practice-token"          # Windows: set OANDA_API_TOKEN=...
export OANDA_PRACTICE_ACCOUNT_ID="your-account-id"    # Windows: set OANDA_PRACTICE_ACCOUNT_ID=...
```

**Paper bot** (`paper_orb_bot.py`) — read-only market data, alerts only, places no orders:

```bash
python3 paper_orb_bot.py --config config.toml --once        # one cycle
python3 paper_orb_bot.py --config config.toml               # continuous (Ctrl-C to stop)
python3 paper_orb_bot.py --config config.toml --once --replay-today   # audit today's candles
```

**Practice executor** (`practice_orb_executor.py`) — submits orders to an OANDA
*practice* account only; it refuses any non-practice URL and requires an explicit
arming flag to place orders:

```bash
python3 practice_orb_executor.py --config practice_execution.toml --status                 # verify access
python3 practice_orb_executor.py --config practice_execution.toml --once                   # dry-run (no orders)
python3 practice_orb_executor.py --config practice_execution.toml --once --confirm-practice-orders   # arm
```

Runtime output (state, events, audit CSVs) is written under `practice_data/` and
`paper_data/`, which are git-ignored.

## Visuals

`docs/strategy.html` and `docs/equity-curve.html` are self-contained — open either
directly in a browser. No server or install needed.

## A word of caution

The executor is practice-only by design. The broker-accurate cost analysis (see
[`RESULTS.md`](RESULTS.md) §8) shows the strategy is profitable only at roughly
0.5-pip-or-tighter all-in spreads and loses money at typical retail spreads. Treat any
run as a paper/practice experiment, not a money-maker, unless your broker's spreads are
genuinely that tight.
