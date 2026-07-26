#!/usr/bin/env python3
"""Simulate the account equity curve for one instrument from a starting balance.

Trades the SHIPPED geometry (per-pair target_points from the config, stop at the
opposite opening-range boundary) chronologically, applying each realized trade's
dollar P&L to a running balance. Reports final balance, return, max drawdown, the
lowest the account ever reached, and whether it was ever wiped out -- because a
positive net total is meaningless if the equity curve went bust on the way there.

P&L model: quote currency is USD (EUR_USD / the inverted USD_JPY both quote USD),
so realized USD = price_change * units. A round-turn spread cost (in points) is
subtracted per trade. Unresolved trades (never hit stop or target within the
session's candles) carry no realized P&L and are reported separately.
"""
from __future__ import annotations

import argparse
from datetime import time as clock_time
from decimal import Decimal
from pathlib import Path

from backtest import candles_from_csv, load_pair_and_bot
from sweep import precompute, q


def run(csv_path: str, instrument: str, start: float, spread_points: float, config: str,
        units_override: int | None = None, risk_pct: float | None = None) -> dict:
    pair, bot = load_pair_and_bot(Path(config), instrument)
    point_size = Decimal(str(pair["point_size"]))
    precision = int(pair["display_precision"])
    fixed_units = int(pair["fixed_units"]) if units_override is None else int(units_override)
    target_pts = Decimal(str(pair.get("target_points", bot["target_points"])))
    cutoff = clock_time(16, 45)

    trades = precompute(candles_from_csv(Path(csv_path)), precision, cutoff)[0]

    balance = start
    peak = start
    min_balance = start
    max_dd = 0.0
    max_dd_pct = 0.0
    wins = losses = unresolved = 0
    busted_after = None
    max_leverage = 0.0
    spread_price = spread_points * float(point_size)  # spread cost per unit, USD

    for day, side, close, idx, rhigh, rlow in trades:
        entry_r = q(close, precision)
        if side == "LONG":
            target = q(close + target_pts * point_size, precision)
            stop = rlow
        else:
            target = q(close - target_pts * point_size, precision)
            stop = rhigh
        if target <= 0 or not (min(stop, target) < entry_r < max(stop, target)):
            continue
        outcome = None
        for c in day[idx + 1:]:
            if side == "LONG":
                if c.low <= stop:
                    outcome = stop; break
                if c.high >= target:
                    outcome = target; break
            else:
                if c.high >= stop:
                    outcome = stop; break
                if c.low <= target:
                    outcome = target; break
        if outcome is None:
            unresolved += 1
            continue
        stop_distance = abs(float(entry_r - stop))
        if risk_pct is not None and stop_distance > 0:
            # Fixed-fractional (compounding): size so a stop-out loses risk_pct of equity.
            units = (risk_pct / 100.0 * balance) / stop_distance
        else:
            units = fixed_units
        signed_price = (outcome - entry_r) if side == "LONG" else (entry_r - outcome)
        pnl = float(signed_price) * units - spread_price * units
        balance += pnl
        if balance > 0:
            max_leverage = max(max_leverage, units * float(entry_r) / balance)
        if outcome == target:
            wins += 1
        else:
            losses += 1
        peak = max(peak, balance)
        dd = peak - balance
        if dd > max_dd:
            max_dd, max_dd_pct = dd, (dd / peak * 100 if peak > 0 else 0)
        min_balance = min(min_balance, balance)
        if balance <= 0 and busted_after is None:
            busted_after = wins + losses

    resolved = wins + losses
    sizing = (f"compounding {risk_pct:g}%/trade" if risk_pct is not None
              else f"fixed {fixed_units} units")
    return {
        "instrument": instrument, "start": start, "final": balance,
        "resolved": resolved, "wins": wins, "losses": losses, "unresolved": unresolved,
        "win_ratio": (wins / resolved * 100) if resolved else 0.0,
        "return_pct": (balance - start) / start * 100,
        "max_dd": max_dd, "max_dd_pct": max_dd_pct, "min_balance": min_balance,
        "busted_after": busted_after, "sizing": sizing, "max_leverage": max_leverage,
        "target_pts": int(target_pts), "spread_points": spread_points,
    }


def show(r: dict) -> None:
    print(f"--- {r['instrument']}  ({r['sizing']}, target={r['target_pts']} pts, "
          f"spread={r['spread_points']:.0f} pts/trade) ---")
    print(f"  start ${r['start']:.2f}  ->  final ${r['final']:.2f}   ({r['return_pct']:+.1f}%)")
    print(f"  resolved trades: {r['resolved']}  (win {r['win_ratio']:.1f}%, {r['wins']}W/{r['losses']}L)"
          f"   unresolved: {r['unresolved']}")
    print(f"  max drawdown: ${r['max_dd']:.2f} ({r['max_dd_pct']:.1f}%)   lowest equity: ${r['min_balance']:.2f}"
          f"   peak leverage: {r['max_leverage']:.1f}x")
    print(f"  wiped out (equity <= $0)? {'YES after trade #'+str(r['busted_after']) if r['busted_after'] else 'no'}")


def main() -> int:
    p = argparse.ArgumentParser(description="Account equity backtest from a starting balance.")
    p.add_argument("--csv", required=True)
    p.add_argument("--instrument", default="EUR_USD")
    p.add_argument("--start", type=float, default=100.0)
    p.add_argument("--spread-points", type=float, default=0.0, help="Round-turn spread cost in points/trade.")
    p.add_argument("--config", default="config.toml")
    p.add_argument("--units", type=int, default=None, help="Override fixed_units (scaled fixed sizing).")
    p.add_argument("--risk-pct", type=float, default=None, help="Compounding: risk this %% of equity per trade.")
    a = p.parse_args()
    show(run(a.csv, a.instrument, a.start, a.spread_points, a.config, a.units, a.risk_pct))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
