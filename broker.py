#!/usr/bin/env python3
"""Broker-accurate fill & slippage model for the opening-range-breakout strategy.

The candle CSVs are MID price (OANDA 'M' candles). Real fills differ, and this
model reproduces the differences with OANDA v20 order semantics:

  * Market ENTRY crosses the spread — buy at the ask (long) / sell at the bid
    (short) — plus a little entry slippage.
  * TAKE-PROFIT is a LIMIT order: it fills at its price, but it TRIGGERS on the
    bid (long) / ask (short). So the mid must travel an extra half-spread to reach
    it — the second half-spread is paid through the trigger, not the fill.
  * STOP-LOSS is a STOP that becomes a MARKET order: it triggers half a spread
    SOONER (bid/ask again) and fills WORSE than its price by a slippage amount —
    and if a candle gaps through the level, it fills at the gapped open.
  * Optional per-trade commission (points-equivalent) for commission pricing.

Net effect vs the idealized backtest: every trade pays ~half a spread on entry,
losers additionally pay stop slippage, and the bid/ask trigger asymmetry makes
targets slightly harder and stops slightly easier to hit. Run this to see whether
the thin edge survives realistic costs.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import time as clock_time
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweep
from backtest import candles_from_csv, load_pair_and_bot

CUTOFF = clock_time(16, 45)


@dataclass
class BrokerModel:
    spread_pts: float = 10.0        # quoted spread (ask - bid), in points; EUR/USD ~1.0 pip retail
    entry_slip_pts: float = 0.5     # extra adverse points on the market entry
    stop_slip_pts: float = 3.0      # extra adverse points when a stop fills at market
    commission_pts: float = 0.0     # round-turn commission in points-equivalent (0 = spread pricing)
    open_widen_mult: float = 1.0    # multiply spread for entries in the first minutes after 10:00
    open_widen_until: clock_time = clock_time(10, 15)

    def spread_at(self, candle) -> float:
        if self.open_widen_mult != 1.0 and candle.new_york_start.time() <= self.open_widen_until:
            return self.spread_pts * self.open_widen_mult
        return self.spread_pts


def resolve(day, i, side, entry_mid, target_mid, stop_mid, pt, m: BrokerModel):
    """Return (kind, net_points) applying broker fills, or None if unresolved."""
    entry_spread = m.spread_at(day[i])
    outcome = None
    for c in day[i + 1:]:
        hs = m.spread_at(c) * pt / 2.0
        lo, hi, op = float(c.low), float(c.high), float(c.open)
        if side == "LONG":
            hit_stop = (lo - hs) <= stop_mid      # bid pierces stop
            hit_tp = (hi - hs) >= target_mid      # bid reaches take-profit
        else:
            hit_stop = (hi + hs) >= stop_mid      # ask pierces stop
            hit_tp = (lo + hs) <= target_mid      # ask reaches take-profit
        if hit_stop:                              # stop-first within a bar (conservative)
            outcome = ("STOP", c); break
        if hit_tp:
            outcome = ("TARGET", c); break
    if outcome is None:
        return None

    kind, c = outcome
    hs_e = entry_spread * pt / 2.0
    hs_x = m.spread_at(c) * pt / 2.0
    comm = m.commission_pts

    if side == "LONG":
        entry_fill = entry_mid + hs_e + m.entry_slip_pts * pt
        if kind == "TARGET":
            exit_fill = target_mid                                   # limit: gets its price
        else:
            gap_open_bid = float(c.open) - hs_x
            exit_fill = min(stop_mid, gap_open_bid) - m.stop_slip_pts * pt
        signed = exit_fill - entry_fill
    else:
        entry_fill = entry_mid - hs_e - m.entry_slip_pts * pt
        if kind == "TARGET":
            exit_fill = target_mid
        else:
            gap_open_ask = float(c.open) + hs_x
            exit_fill = max(stop_mid, gap_open_ask) + m.stop_slip_pts * pt
        signed = entry_fill - exit_fill
    return kind, signed / pt - comm


def run(csv_path, instrument, config, m: BrokerModel, target=None, start=1000.0, units=10000):
    pair, bot = load_pair_and_bot(Path(config), instrument)
    ps = Decimal(str(pair["point_size"])); prec = int(pair["display_precision"]); pt = float(ps)
    T = Decimal(str(target if target is not None else pair.get("target_points", bot["target_points"])))
    trades = sweep.precompute(candles_from_csv(Path(csv_path)), prec, CUTOFF)[0]

    wins = losses = unresolved = 0
    net_pts = 0.0
    bal = start; peak = start; maxdd = 0.0
    for day, side, close, idx, rhigh, rlow in trades:
        entry_mid = float(sweep.q(close, prec))
        if side == "LONG":
            target_mid = float(sweep.q(close + T * ps, prec)); stop_mid = float(rlow)
        else:
            target_mid = float(sweep.q(close - T * ps, prec)); stop_mid = float(rhigh)
        if target_mid <= 0 or not (min(stop_mid, target_mid) < entry_mid < max(stop_mid, target_mid)):
            continue
        r = resolve(day, idx, side, entry_mid, target_mid, stop_mid, pt, m)
        if r is None:
            unresolved += 1; continue
        kind, pts = r
        net_pts += pts
        bal += pts * pt * units
        peak = max(peak, bal); maxdd = max(maxdd, (peak - bal) / peak * 100 if peak > 0 else 0)
        wins += kind == "TARGET"; losses += kind == "STOP"
    resolved = wins + losses
    return {
        "target": int(T), "win": (wins / resolved * 100) if resolved else 0.0,
        "exp": (net_pts / resolved) if resolved else 0.0, "resolved": resolved,
        "unresolved": unresolved, "final": bal, "ret": (bal - start) / start * 100, "maxdd": maxdd,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Broker-accurate slippage model for the ORB strategy.")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--instrument", default="EUR_USD")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--target", type=int, default=None, help="Override target (default: config value).")
    ap.add_argument("--spread-pts", type=float, default=10.0)
    ap.add_argument("--entry-slip-pts", type=float, default=0.5)
    ap.add_argument("--stop-slip-pts", type=float, default=3.0)
    ap.add_argument("--commission-pts", type=float, default=0.0)
    ap.add_argument("--start", type=float, default=1000.0)
    ap.add_argument("--units", type=int, default=10000)
    ap.add_argument("--sweep-spread", action="store_true", help="Print a spread sensitivity table instead.")
    a = ap.parse_args()

    def one(m, label):
        r = run(a.csv, a.instrument, a.config, m, a.target, a.start, a.units)
        print(f"  {label:<26} win {r['win']:5.1f}%   exp {r['exp']:+6.2f} pts   "
              f"${a.start:.0f}->${r['final']:,.0f} ({r['ret']:+.0f}%)   maxDD {r['maxdd']:.0f}%   "
              f"trades {r['resolved']}")

    tgt = a.target if a.target is not None else "config"
    print(f"\n{a.instrument} · target={tgt} · broker-accurate fills "
          f"(entry_slip={a.entry_slip_pts}, stop_slip={a.stop_slip_pts}, comm={a.commission_pts} pts)\n")
    if a.sweep_spread:
        for s in (0, 2, 6, 8, 10, 12, 14):
            one(BrokerModel(spread_pts=s, entry_slip_pts=a.entry_slip_pts,
                            stop_slip_pts=a.stop_slip_pts, commission_pts=a.commission_pts),
                f"spread {s} pts ({s/10:.1f} pip)")
    else:
        one(BrokerModel(spread_pts=a.spread_pts, entry_slip_pts=a.entry_slip_pts,
                        stop_slip_pts=a.stop_slip_pts, commission_pts=a.commission_pts),
            f"spread {a.spread_pts} pts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
