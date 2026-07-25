#!/usr/bin/env python3
"""Backtest the opening-range-breakout strategy and report its win ratio.

This harness reuses the REAL strategy logic from paper_orb_bot
(OpeningRangeStrategy: entry, stop/target, and the conservative stop-first
intrabar exit) so the results reflect the shipped behavior, not a
reimplementation.

Data sources (pick one):
  --csv PATH        M15 candles as CSV with columns: time,open,high,low,close
                    (time is ISO-8601 UTC, e.g. 2026-01-05T14:30:00Z). One file
                    per instrument; pass --instrument to name it.
  --synthetic N     Generate N weekday sessions with a random walk. THIS IS A
                    MECHANISM CHECK ONLY -- the win ratio it prints is an
                    artifact of the generator, not the strategy's real edge.

Win ratio is computed over RESOLVED trades only:
    win_ratio = TARGET_exits / (TARGET_exits + STOP_exits)
Trades still OPEN at the entry cutoff are reported separately as UNRESOLVED,
because the strategy (by design) evaluates exits only inside the entry window.
"""
from __future__ import annotations

import argparse
import csv
import random
import tomllib
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from paper_orb_bot import (
    Candle,
    NEW_YORK,
    UTC,
    OpeningRangeStrategy,
    make_fresh_pair_state,
)


class CollectingRecorder:
    """Silent recorder that only captures closed trades for tallying."""

    def __init__(self) -> None:
        self.closed: list[Any] = []

    def event(self, *args: Any, **kwargs: Any) -> None:
        pass

    def save_state(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_closed_trade(self, trade: Any) -> None:
        self.closed.append(trade)


def load_pair_and_bot(config_path: Path, instrument: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    bot = config["bot"]
    bot.setdefault("max_trades_per_day", 1)
    for pair in config["pairs"]:
        if str(pair["instrument"]) == instrument:
            return pair, bot
    raise SystemExit(f"Instrument {instrument} not found in {config_path}")


def candles_from_csv(path: Path) -> list[Candle]:
    candles: list[Candle] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            start = datetime.fromisoformat(row["time"].replace("Z", "+00:00")).astimezone(UTC)
            candles.append(
                Candle(
                    start=start,
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                )
            )
    return sorted(candles, key=lambda c: c.start)


def synthetic_candles(sessions: int, point_size: Decimal, seed: int = 7) -> list[Candle]:
    """Random-walk M15 candles, 09:30..19:45 NY, for `sessions` weekdays.

    Deliberately simple: a per-candle Gaussian step a few points in size. Not a
    market model -- only enough structure to exercise the harness end to end.
    The window extends past the 16:45 entry cutoff so open positions have candles
    on which to reach their stop/target after the cutoff.
    """
    rng = random.Random(seed)
    step = float(point_size) * 6.0  # ~6 points of noise per bar
    candles: list[Candle] = []
    day = date(2025, 1, 6)  # a Monday
    made = 0
    price = 1.10
    while made < sessions:
        if day.isoweekday() <= 5:
            # 09:30 through 19:45 inclusive = 42 M15 bars (spans past the cutoff)
            local = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NEW_YORK)
            for _ in range(42):
                o = price
                c = o + rng.gauss(0, step)
                hi = max(o, c) + abs(rng.gauss(0, step)) * 0.5
                lo = min(o, c) - abs(rng.gauss(0, step)) * 0.5
                q = float(point_size)
                candles.append(
                    Candle(
                        start=local.astimezone(UTC),
                        open=Decimal(str(round(o / q) * q)),
                        high=Decimal(str(round(hi / q) * q)),
                        low=Decimal(str(round(lo / q) * q)),
                        close=Decimal(str(round(c / q) * q)),
                    )
                )
                price = c
                local += timedelta(minutes=15)
            made += 1
        day += timedelta(days=1)
    return candles


def group_by_session(candles: Iterable[Candle]) -> dict[date, list[Candle]]:
    days: dict[date, list[Candle]] = defaultdict(list)
    for candle in candles:
        days[candle.new_york_start.date()].append(candle)
    for day in days.values():
        day.sort(key=lambda c: c.start)
    return dict(sorted(days.items()))


def backtest(candles: list[Candle], pair: dict[str, Any], bot: dict[str, Any]) -> dict[str, Any]:
    point_size = Decimal(str(pair["point_size"]))
    wins = losses = unresolved = no_signal_days = 0
    total_points = Decimal(0)
    for session_date, day_candles in group_by_session(candles).items():
        recorder = CollectingRecorder()
        strategy = OpeningRangeStrategy(pair, bot, recorder)
        pair_state = make_fresh_pair_state(session_date)
        if not strategy.establish_range(day_candles, session_date, pair_state):
            continue
        before = len(recorder.closed)
        for candle in day_candles:
            strategy.process_candle(candle, pair_state)
        closed_today = recorder.closed[before:]
        if closed_today:
            trade = closed_today[-1]
            entry = Decimal(trade.entry_price)
            exit_price = Decimal(trade.exit_price)
            signed = (exit_price - entry) if trade.side == "LONG" else (entry - exit_price)
            total_points += signed / point_size
            if trade.exit_reason == "TARGET":
                wins += 1
            else:
                losses += 1
        else:
            trade_payload = pair_state.get("trade")
            if trade_payload and trade_payload.get("status") == "OPEN":
                unresolved += 1
            else:
                no_signal_days += 1
    resolved = wins + losses
    win_ratio = (wins / resolved) if resolved else 0.0
    expectancy = (total_points / resolved) if resolved else Decimal(0)
    return {
        "wins": wins,
        "losses": losses,
        "resolved": resolved,
        "unresolved": unresolved,
        "no_signal_days": no_signal_days,
        "win_ratio": win_ratio,
        "net_points": total_points,
        "expectancy_points": expectancy,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest the ORB strategy and report win ratio.")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--instrument", default="EUR_USD")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", type=Path, help="M15 candle CSV: time,open,high,low,close")
    source.add_argument("--synthetic", type=int, metavar="N", help="Generate N weekday sessions (MECHANISM CHECK ONLY)")
    args = parser.parse_args()

    pair, bot = load_pair_and_bot(args.config, args.instrument)
    if args.csv:
        candles = candles_from_csv(args.csv)
        label = f"real CSV data ({args.csv})"
    else:
        candles = synthetic_candles(args.synthetic, Decimal(str(pair["point_size"])))
        label = f"SYNTHETIC random-walk data ({args.synthetic} sessions) -- NOT a real result"

    result = backtest(candles, pair, bot)
    print(f"Instrument : {args.instrument}")
    print(f"Data       : {label}")
    print(f"Sessions   : {len(group_by_session(candles))}")
    print(f"Wins (TP)  : {result['wins']}")
    print(f"Losses (SL): {result['losses']}")
    print(f"Unresolved : {result['unresolved']} (still open at the end of the session's candle data)")
    print(f"No-signal  : {result['no_signal_days']} sessions with a range but no breakout")
    if result["resolved"]:
        print(f"WIN RATIO  : {result['win_ratio']*100:.1f}%  ({result['wins']}/{result['resolved']} resolved)")
        print(f"Net points : {result['net_points']:.1f}")
        print(f"Expectancy : {result['expectancy_points']:.2f} points/resolved-trade")
    else:
        print("WIN RATIO  : n/a (no resolved trades)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
