# ORB + Heikin-Ashi Confirmation (Pine Script v6)

`orb_ha_strategy.pine` is an intraday Opening Range Breakout `strategy()` with four
selectable entry variants, so each one can be backtested on exactly the same data.

## Install

1. TradingView → Pine Editor → paste `orb_ha_strategy.pine` → **Add to chart**.
2. Use a **normal candlestick chart** on 1-min or 5-min. Heikin-Ashi is computed inside
   the script. Don't switch the chart to HA, because a strategy on an HA chart fills at
   synthetic prices.
3. **Properties tab:** set commission and slippage for your instrument. The defaults are
   0.01 % per side and 2 ticks. For futures, switch to cash per contract.

## Variants

| # | Entry rule |
|---|------------|
| 1 | Raw close beyond the OR high or low |
| 2 | HA close beyond the OR level, correct HA color, wrong-side wick ≤ 10 % of HA range, body ≥ 30 % |
| 3 | Variant 2, but only on days when OR width ≤ 1.0 × prior-day ATR(14) |
| 4 | Raw close breaks the level, then within 12 bars price retests it and an HA candle closes back beyond it |

The rules below apply to every variant:

- **Stop:** the HA candle extreme by default, or the OR midline or the opposite OR side.
- **TP1** is the OR level ± 1 × OR range, and **TP2** is the OR level ± 2 × OR range.
- At TP1, 50 % of the position closes and the runner's stop moves to break-even.
- An optional HA trail can close the runner after TP1.
- **Hard exit at 11:00 ET.** No new entry is allowed if it would fill at or after that time.
- The strategy takes at most 2 trades a day, 1 per direction, and holds only one position at a time.
- A signal is skipped if the close is already beyond TP1.

The header comment in the script lists every same-bar and fill assumption (A–I).

## Backtest protocol (do this per variant)

1. **Use identical settings for every variant.** Keep the same symbol, timeframe, date
   range, costs and sizing, and change only *Entry variant*.
2. **Use enough data.** Aim for ≥ 2 years and ≥ 100 trades per variant. Fewer than about
   30 trades is noise. On 1-min data, TradingView's bar limit may cap history, so note
   the date range you actually got.
3. **Run 1-min and 5-min.** A large gap between them means the result depends on
   intrabar path assumptions (assumption C).
4. **Split the data.** Tune nothing on the last ~30 % of history. Run that period once,
   at the end.
5. **Test on more than one symbol.** Use at least one other instrument class.

### Report back, per variant

| Field | Where it comes from |
|---|---|
| Symbol, TF, date range | chart |
| Net profit, # trades, win %, profit factor, max drawdown | Strategy Tester → Overview |
| Avg R, PF (R), Max DD (R), total R | on-chart stats table (net of costs) |
| Skipped bars (TP1 / size) | stats table |
| Avg trade length, % of exits by reason (SL / TP1 / TP2 / BE / Time / HA trail) | List of Trades (exit comments) |

Use **R-based** figures when comparing variants. They don't depend on position size or
compounding.
