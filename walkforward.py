#!/usr/bin/env python3
"""Rolling walk-forward validation for the EUR/USD opening-range-breakout strategy.

Each fold re-optimizes the target on an in-sample TRAIN window (the target with the
best net-of-spread expectancy among those clearing a >76% win rate), then applies it
BLIND to the immediately following out-of-sample TEST window. Concatenating the test
windows gives a genuine out-of-sample track record: if the re-chosen target keeps
landing near the same value and the test windows stay >76% and profitable, the edge
is not an artifact of one lucky split.

Entry is independent of the target, so trades are precomputed once and only the
stop/target/exit are re-simulated per window (fast).
"""
from __future__ import annotations

import argparse
import json
from datetime import time as clock_time, timedelta
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep
from backtest import candles_from_csv, load_pair_and_bot

CUTOFF = clock_time(16, 45)
CANDIDATE_TARGETS = [20, 25, 30, 35, 40, 45, 50]
WIN_FLOOR = 76.0


def trade_date(tr):
    return tr[0][0].new_york_start.date()


def choose_target(train, ps, prec, spread):
    """Best net-expectancy target clearing the win floor; fall back to best net if none."""
    best = best_any = None
    for T in CANDIDATE_TARGETS:
        r = sweep.simulate(train, T, None, ps, prec)
        if r["resolved"] < 50:
            continue
        net = float(r["exp"]) - spread
        if best_any is None or net > best_any[0]:
            best_any = (net, T)
        if r["win_ratio"] * 100 > WIN_FLOOR and net > 0:
            if best is None or net > best[0]:
                best = (net, T)
    return (best or best_any)[1]


def per_trade(trades, T, ps, prec, units, spread_points):
    """Chronological (date, usd_pnl) for resolved trades at target T."""
    out = []
    spread_price = spread_points * float(ps)
    for day, side, close, idx, rhigh, rlow in trades:
        entry_r = sweep.q(close, prec)
        if side == "LONG":
            target = sweep.q(close + Decimal(T) * ps, prec); stop = rlow
        else:
            target = sweep.q(close - Decimal(T) * ps, prec); stop = rhigh
        if target <= 0 or not (min(stop, target) < entry_r < max(stop, target)):
            continue
        outcome = None
        for c in day[idx + 1:]:
            if side == "LONG":
                if c.low <= stop: outcome = stop; break
                if c.high >= target: outcome = target; break
            else:
                if c.high >= stop: outcome = stop; break
                if c.low <= target: outcome = target; break
        if outcome is None:
            continue
        signed = (outcome - entry_r) if side == "LONG" else (entry_r - outcome)
        out.append((day[0].new_york_start.date().isoformat(), float(signed) * units - spread_price * units))
    return out


def curve_stats(series, start):
    bal = start; peak = start; max_dd = 0.0; wins = 0
    curve = [(series[0][0] if series else None, start)]
    for d, pnl in series:
        bal += pnl
        curve.append((d, bal))
        peak = max(peak, bal)
        max_dd = max(max_dd, (peak - bal) / peak * 100 if peak > 0 else 0)
        wins += 1 if pnl > 0 else 0
    n = len(series)
    return {
        "final": bal, "return_pct": (bal - start) / start * 100,
        "trades": n, "win_ratio": (wins / n * 100) if n else 0,
        "max_dd_pct": max_dd, "curve": curve,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Rolling walk-forward validation.")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--instrument", default="EUR_USD")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--spread-points", type=float, default=2.0)
    ap.add_argument("--start", type=float, default=1000.0)
    ap.add_argument("--units", type=int, default=10000)
    ap.add_argument("--train-days", type=int, default=540)
    ap.add_argument("--test-days", type=int, default=180)
    ap.add_argument("--emit-curve", metavar="PATH", help="Write OOS equity curve JSON here.")
    a = ap.parse_args()

    pair, bot = load_pair_and_bot(Path(a.config), a.instrument)
    ps = Decimal(str(pair["point_size"])); prec = int(pair["display_precision"])
    trades = sweep.precompute(candles_from_csv(Path(a.csv)), prec, CUTOFF)[0]
    trades.sort(key=trade_date)
    d0, d1 = trade_date(trades[0]), trade_date(trades[-1])

    train_d = timedelta(days=a.train_days); test_d = timedelta(days=a.test_days)
    print(f"Walk-forward: {a.instrument}  {d0} -> {d1}   "
          f"train={a.train_days}d, test={a.test_days}d, roll={a.test_days}d, spread={a.spread_points:g} pts\n")
    print(f"{'#':>2} {'train window':>25} {'->target':>8} {'trainWin':>8} | "
          f"{'test window':>25} {'testWin':>8} {'netExp':>7} {'trades':>7}")

    oos_series = []
    picks = []
    t0 = d0
    fold = 0
    while t0 + train_d + test_d <= d1 + timedelta(days=1):
        tr_lo, tr_hi = t0, t0 + train_d
        te_lo, te_hi = tr_hi, tr_hi + test_d
        train = [t for t in trades if tr_lo <= trade_date(t) < tr_hi]
        test = [t for t in trades if te_lo <= trade_date(t) < te_hi]
        if len(train) >= 80 and test:
            fold += 1
            T = choose_target(train, ps, prec, a.spread_points)
            picks.append(T)
            rtr = sweep.simulate(train, T, None, ps, prec)
            rte = sweep.simulate(test, T, None, ps, prec)
            oos_series += per_trade(test, T, ps, prec, a.units, a.spread_points)
            print(f"{fold:>2} {str(tr_lo)+'..'+str(tr_hi):>25} {T:>8} {rtr['win_ratio']*100:>7.1f}% | "
                  f"{str(te_lo)+'..'+str(te_hi):>25} {rte['win_ratio']*100:>7.1f}% "
                  f"{float(rte['exp'])-a.spread_points:>+7.2f} {rte['resolved']:>7}")
        t0 += test_d

    st = curve_stats(oos_series, a.start)
    from statistics import mean
    print("\n--- Aggregated OUT-OF-SAMPLE (concatenated test windows) ---")
    print(f"  folds: {fold}   chosen targets: {picks}  (avg {mean(picks):.0f})")
    print(f"  OOS trades: {st['trades']}   win rate: {st['win_ratio']:.1f}%")
    print(f"  ${a.start:.0f} @ {a.units} units, {a.spread_points:g}-pt spread  ->  "
          f"${st['final']:.2f}  ({st['return_pct']:+.1f}%)   max drawdown {st['max_dd_pct']:.1f}%")

    if a.emit_curve:
        Path(a.emit_curve).write_text(json.dumps(
            {"start": a.start, "units": a.units, "spread": a.spread_points,
             "oos": [[d, round(b, 2)] for d, b in st["curve"]]}), encoding="utf-8")
        print(f"  wrote OOS equity curve -> {a.emit_curve}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
