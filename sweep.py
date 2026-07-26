#!/usr/bin/env python3
"""Sweep target_points x stop-cap over real candles; find profitable geometry (if any).

Entry rule is unchanged (first breakout close beyond the opening range) and is
independent of target/stop, so it is precomputed once per day. Only stop/target
and the stop-first conservative exit depend on the swept parameters. The
simulator is validated against the shipped backtest at the baseline before any
swept cell is trusted.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from paper_orb_bot import RANGE_START, RANGE_END, ENTRY_START, decimal_text
from backtest import candles_from_csv, group_by_session, load_pair_and_bot

CUTOFF_HH, CUTOFF_MM = 16, 45  # entry_cutoff_new_york = "16:45"


def q(value: Decimal, precision: int) -> Decimal:
    return Decimal(decimal_text(value, precision))


def precompute(candles, precision, entry_cutoff):
    """Per trading day: (day_candles, side, entry_close, entry_idx, range_high, range_low)."""
    trades = []
    no_breakout = 0
    for _, day in group_by_session(candles).items():
        rc = [c for c in day if c.new_york_start.time() in (RANGE_START, RANGE_END)]
        if {RANGE_START, RANGE_END} - {c.new_york_start.time() for c in rc}:
            continue
        rhigh = q(max(c.high for c in rc), precision)
        rlow = q(min(c.low for c in rc), precision)
        entry = None
        for i, c in enumerate(day):
            t = c.new_york_start.time()
            if t < ENTRY_START or t > entry_cutoff:
                continue
            if c.close > rhigh:
                entry = ("LONG", c.close, i); break
            if c.close < rlow:
                entry = ("SHORT", c.close, i); break
        if entry is None:
            no_breakout += 1
            continue
        trades.append((day, entry[0], entry[1], entry[2], rhigh, rlow))
    return trades, no_breakout


def simulate(trades, target_pts, cap_pts, point_size, precision):
    point = point_size
    T = Decimal(target_pts)
    wins = losses = unresolved = 0
    net = Decimal(0)
    for day, side, close, idx, rhigh, rlow in trades:
        entry_r = q(close, precision)
        if side == "LONG":
            target = q(close + T * point, precision)
            stop = rlow if cap_pts is None else q(max(rlow, close - Decimal(cap_pts) * point), precision)
        else:
            target = q(close - T * point, precision)
            stop = rhigh if cap_pts is None else q(min(rhigh, close + Decimal(cap_pts) * point), precision)
        if target <= 0 or not (min(stop, target) < entry_r < max(stop, target)):
            continue
        outcome = None
        for c in day[idx + 1:]:
            if side == "LONG":
                if c.low <= stop:
                    outcome = ("STOP", stop); break
                if c.high >= target:
                    outcome = ("TARGET", target); break
            else:
                if c.high >= stop:
                    outcome = ("STOP", stop); break
                if c.low <= target:
                    outcome = ("TARGET", target); break
        if outcome is None:
            unresolved += 1
            continue
        kind, px = outcome
        signed = (px - entry_r) if side == "LONG" else (entry_r - px)
        net += signed / point
        if kind == "TARGET":
            wins += 1
        else:
            losses += 1
    resolved = wins + losses
    return {
        "wins": wins, "losses": losses, "unresolved": unresolved, "resolved": resolved,
        "win_ratio": (wins / resolved) if resolved else 0.0,
        "net": net,
        "exp": (net / resolved) if resolved else Decimal(0),
    }


def run(csv_path, instrument, config="config.toml"):
    pair, bot = load_pair_and_bot(Path(config), instrument)
    point_size = Decimal(str(pair["point_size"]))
    precision = int(pair["display_precision"])
    from datetime import time as clock_time
    cutoff = clock_time(CUTOFF_HH, CUTOFF_MM)
    candles = candles_from_csv(Path(csv_path))
    trades, no_breakout = precompute(candles, precision, cutoff)

    # ---- validate baseline against shipped numbers ----
    base = simulate(trades, 10, None, point_size, precision)
    print(f"\n===== {instrument} =====")
    print(f"trading days with a breakout: {len(trades)}   (no-breakout days: {no_breakout})")
    print(f"[validate baseline T=10 cap=None] wins={base['wins']} losses={base['losses']} "
          f"win={base['win_ratio']*100:.1f}% net={base['net']:.0f} exp={base['exp']:.2f} pts")

    TARGETS = [10, 15, 20, 30, 50, 75, 100, 150, 200]
    CAPS = [None, 200, 150, 100, 75, 50, 30, 20]
    # expectancy (points) matrix
    print("\nExpectancy (gross points/trade) — rows: target_pts, cols: stop cap")
    header = "  T\\cap |" + "".join(f"{('none' if c is None else c):>8}" for c in CAPS)
    print(header)
    print("  " + "-" * (len(header) - 2))
    best = None
    for T in TARGETS:
        cells = []
        for cap in CAPS:
            r = simulate(trades, T, cap, point_size, precision)
            cells.append(r["exp"])
            if r["resolved"] >= 200 and (best is None or r["exp"] > best[0]):
                best = (r["exp"], T, cap, r)
        print(f"  {T:>5} |" + "".join(f"{float(e):>8.2f}" for e in cells))
    e, T, cap, r = best
    print(f"\n  BEST gross expectancy: {float(e):+.2f} pts/trade  at target={T}, cap={cap}")
    print(f"    -> win={r['win_ratio']*100:.1f}%  trades={r['resolved']}  net={r['net']:.0f} pts")
    return best


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Sweep target_points x stop-cap over real candles.")
    parser.add_argument("--csv", required=True, help="M15 candle CSV: time,open,high,low,close (UTC)")
    parser.add_argument("--instrument", default="EUR_USD")
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args()
    run(args.csv, args.instrument, args.config)
