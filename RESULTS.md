# Strategy backtest results

Backtest of the 15-minute New York opening-range-breakout strategy on **5 years of
real M15 data** (~2021-07 to 2026-07, 1,565 sessions per pair). All figures use the
**shipped strategy code** (`OpeningRangeStrategy`), not a reimplementation, and were
produced by the tools in this repo (`backtest.py`, `sweep.py`, `equity.py`).

Units: **10 points = 1 pip** for both pairs. Expectancy is **gross** (mid-price, before
spread) unless noted. The USD/JPY series was inverted from a `JPYUSD` source to match
the `USD_JPY` config.

## 1. The shipped config wins constantly but loses money

With the original `target_points = 10` (a ~1-pip target):

| Pair | Win ratio | Net | Expectancy |
|------|-----------|-----|------------|
| EUR/USD | **94.0%** (1199/1276) | −761 pts | **−0.60 pts/trade** |
| USD/JPY | **93.1%** (1170/1257) | −6,840 pts | **−5.44 pts/trade** |

The win ratio is a trap: every win is a fixed +10 pts (~1 pip), but every loss is the
full opening-range width (~16 pts on EUR/USD, ~21 pts on USD/JPY). The rare losses
erase the many tiny wins. **Judge this strategy on expectancy, not win ratio.**

## 2. Parameter sweep — is any geometry profitable?

Gross expectancy (points/trade) over `target_points` × stop-cap, EUR/USD:

```
 T\cap   none    200    150    100     75     50     30
   10   -0.60  -2.98  -4.92 -10.55 -13.82 -16.30 -14.72   ← shipped (worst region)
   30   +4.36  +1.26  -0.34  -4.84  -6.10  -8.09  -8.21
   75  +12.23  +7.44  +4.74  +1.39  +0.77  +0.10  +0.77
  150  +20.82 +10.39  +6.18  +2.70  +2.90  +2.46  +2.50   ← best: +20.8 pts, 60.8% win
  200  +19.47  +7.59  +4.14  +0.32  +1.10  +2.23  +2.25
```

Findings:
- **The lever is the target, not the stop.** Expectancy rises monotonically with target
  size to a ~150-pt plateau (a plateau, not a spiky peak — consistent with a real
  breakout-momentum effect rather than noise).
- **Stop caps universally hurt** (every capped column is worse than "none," both pairs).
  Tightening the stop increases stop-outs faster than it shrinks the loss.
- **USD/JPY has no robustly profitable cell** — full-sample best was a marginal +1.7 pts.

## 3. Out-of-sample check (tune on first 70%, test on last 30%)

| Pair | Best-on-train | In-sample | Out-of-sample |
|------|---------------|-----------|---------------|
| EUR/USD | target=150, no cap | +22.9 pts, 62.0% | **+16.3 pts, 57.9% win, +5,331 net** |
| USD/JPY | target=200, cap=75 | −0.66 pts | **−11.0 pts** (already negative in-sample) |

The EUR/USD edge **survives on unseen years**. USD/JPY does not — no target was profitable
in-sample or out.

## 4. Decision applied to the config

Two profitable EUR/USD tunings emerged, optimizing different objectives:

| Objective | Target | Win rate | Net expectancy | Note |
|-----------|--------|----------|----------------|------|
| Max total profit | 150 | ~61% | +18.8 net pts | most money, but wins only ~61% |
| **Win rate >76% + profit (chosen)** | **40** | **~84%** | **+5.7 net pts** | high, steady win rate |

- **EUR/USD → `target_points = 40`** (per-pair override) — chosen to meet a
  >76% win-rate objective while staying profitable.
- **USD/JPY → keeps the default 10** (no profitable geometry found; consider not trading it).

Both programs support a per-pair `target_points` that overrides the global
`bot.target_points`.

### Win-rate-constrained search (target with no stop cap)

Every tested target of ~30-50 clears >76% win AND positive net-of-spread
expectancy, in-sample, out-of-sample, and in the trailing year:

| Target | Full 5yr | Out-of-sample (last 30%) | Past 12 months |
|--------|----------|--------------------------|----------------|
| 30 | 87.0% / +2.4 | 86.8% / +2.1 | 85.8% / +3.6 |
| **40 (chosen)** | 84.0% / +5.7 | 83.0% / +4.7 | 81.0% / +4.8 |
| 50 | 80.9% / +6.5 | 79.4% / +4.6 | 76.8% / +3.5 |

(win% / net-of-spread points per trade). Target 40 gives the best balance of a
comfortable margin above 76% and steady profit. Stop caps were worse at every
target. The mechanism: a ~40-pt target is ~4x the original, hit ~84% of the
time, so frequent wins finally outweigh the rare full-range losses.

## 5. Account equity backtest — EUR/USD from $100

> Note: the equity figures in sections 5-6 were computed with the earlier
> max-profit target of 150. With the chosen target of 40, $1,000 at 10,000
> units (0.2-pip spread) grows to ~$1,713 (+71%) with a ~18% max drawdown —
> lower total profit than 150, but an ~84% win rate. Rerun any row with
> `equity.py` against the current config to refresh.

Shipped sizing (`fixed_units = 1000`, non-compounding), traded chronologically:

| Spread assumption | Final balance | Return | Max drawdown | Lowest equity | Wiped out? |
|-------------------|---------------|--------|--------------|---------------|------------|
| 0 (gross) | **$318.63** | +218.6% | $27.39 (11.2%) | $83.85 | no |
| 0.2 pip (2 pts) | $297.63 | +197.6% | $28.43 (12.0%) | $82.59 | no |
| 0.5 pip (5 pts) | $266.13 | +166.1% | $29.99 (13.5%) | $80.70 | no |

Over 1,050 resolved trades (60.8% win). The account never approached ruin; 1,000 units
of EUR/USD is ~11:1 leverage on $100, and the lowest equity (~$81) stays far above a
50:1 margin requirement (~$23).

## 6. Position sizing comparison (EUR/USD, $1,000 start, 0.2-pip spread)

Same 1,050 trades, different sizing:

| Sizing | Final | Return | Max drawdown | Peak leverage | Realistic? |
|--------|-------|--------|--------------|---------------|------------|
| Fixed 1,000 units | $1,198 | +20% | 2.5% | 1.2x | yes |
| Fixed 10,000 units (scaled) | $2,976 | +198% | 12.0% | 13.8x | yes |
| Compounding 1%/trade | $1,956 | +96% | 12.3% | 25x | borderline |
| Compounding 2%/trade | $3,506 | +251% | 23.6% | 50x | at broker limit |
| Compounding 5%/trade | $11,936 | +1094% | 53.0% | 130x | no |

- Return and drawdown scale together; there is no free lunch.
- **At equal ~12% drawdown, scaled fixed sizing (+198%) beats compounding at 1% (+96%).**
- Compounding sizes each trade to risk a fixed % of equity; on tight-opening-range days
  the stop is close, so required leverage spikes (25-130x). Values above ~30-50x exceed
  typical retail broker limits, so the 2%/5% rows are optimistic — a real broker would
  reject those sizes. Only the fixed-unit modes (<=14x) are fully executable.

Run these with `equity.py --units N` (scaled) or `equity.py --risk-pct X` (compounding).

## 7. Rolling walk-forward validation (target re-optimized each fold)

Gold-standard test: on each fold the target is re-chosen on an 18-month training
window (best net-of-spread expectancy clearing >76% win), then applied blind to the
next 6 months. Run with `walkforward.py`.

| Test window | Chosen target | Test win% | Net exp (pts) |
|-------------|---------------|-----------|---------------|
| 2023-01 → 07 | 50 | 79.5% | −1.41 |
| 2023-07 → 2024-01 | 40 | 86.8% | +0.66 |
| 2024-01 → 07 | 40 | 78.7% | +4.70 |
| 2024-07 → 2025-01 | 40 | 82.8% | +8.56 |
| 2025-01 → 07 | 50 | 86.3% | +9.95 |
| 2025-07 → 2026-01 | 50 | 77.0% | −0.48 |
| 2026-01 → 06 | 40 | 79.2% | +1.15 |

- Re-optimized target consistently landed on **40–50 (avg 44)** — 40 is a sound central choice.
- **Win rate cleared 76% in all 7 out-of-sample windows.** Robust.
- Aggregate out-of-sample ($1,000 @ 10,000 units, 0.2-pip spread): **$1,285 (+28.5%)** over 853
  OOS trades, **81.5% win, 17% max drawdown**.
- Profitability is real on aggregate but **thin** — 2 of 7 windows were marginally negative. A
  dependable high-win-rate strategy with a small edge, not a money printer.

## Caveats

- **Gross, mid-price.** Real fills pay spread + slippage; the table above brackets it,
  and EUR/USD stays clearly positive.
- **Non-compounding.** Fixed 1,000 units, so returns are roughly linear; risk-based
  sizing would raise both return and drawdown.
- **233 unresolved EUR/USD trades** (never hit stop or target within the session's
  candles) carry no realized P&L and are excluded — a real limitation of the strategy's
  intraday exit logic.
- **One train/test split**, not a full rolling walk-forward. The `150` came from an
  in-sample grid search that was then checked once out-of-sample.

## 8. Broker-accurate costs — the reality check (`broker.py`)

The sections above are gross of slippage. `broker.py` models OANDA v20 fills on the
mid-price data: market entry crosses to the ask/bid + slippage; take-profit is a limit
(fills at price but must be *reached* by the bid/ask, paying the second half-spread via
the trigger); stop-loss triggers half a spread sooner and fills worse by a slippage
amount (or at a gapped open). EUR/USD, target 40, $1,000 @ 10,000 units:

| All-in spread | Win rate | Expectancy | $1,000 → | Verdict |
|---------------|----------|------------|----------|---------|
| 0.0 pip | 84.0% | +6.74 pts | $1,840 | ideal |
| 0.2 pip | 83.5% | +4.27 pts | $1,532 | profitable |
| ~0.5 pip | ~82% | ≈ 0 | ~$1,000 | break-even |
| 0.6 pip | 82.4% | −0.28 pts | $965 | loses |
| 1.0 pip (typical retail) | 81.3% | −5.13 pts | $362 | loses badly |
| 1.4 pip | 80.6% | −8.38 pts | −$42 | blown |

- **Break-even is ~0.5 pip all-in.** The strategy is profitable only at ECN/raw-spread
  costs (~0.2–0.4 pip in liquid NY hours); at standard ~1-pip retail spread it is a clear
  net loser.
- **Win rate barely moves (81–84%) while profitability flips sign** — the definitive proof
  that the ~84% win rate is not an edge. Wins shrink by ~half a spread; the rare, large
  losses grow by half a spread + stop slippage.
- Defaults (0.5-pt entry slip, 3-pt stop slip) are lenient, so this is not a pessimistic case.

Run: `python3 broker.py --csv EURUSD_15m.csv --instrument EUR_USD --sweep-spread`.

## Visuals

Standalone pages in [`docs/`](docs/): [`strategy.html`](docs/strategy.html) diagrams the
strategy; [`equity-curve.html`](docs/equity-curve.html) plots the target-40 equity curve against
the walk-forward out-of-sample curve.

## Reproduce

```
# Win ratio + expectancy on real data
python3 backtest.py --csv EURUSD_15m.csv --instrument EUR_USD

# Parameter sweep + out-of-sample check (edit paths at the bottom of the file)
python3 sweep.py

# Account equity from a starting balance
python3 equity.py --csv EURUSD_15m.csv --instrument EUR_USD --start 100 --spread-points 2
```

CSV columns: `time,open,high,low,close` (time ISO-8601 UTC). A `JPYUSD` source must be
inverted (`price -> 1/price`, swapping high/low) to trade as `USD_JPY`.
