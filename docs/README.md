# Visuals

Standalone, self-contained HTML pages (no external requests; open directly in a browser).
Both adapt to light/dark mode.

| File | What it shows |
|------|---------------|
| [`strategy.html`](strategy.html) | Diagram of the EUR/USD opening-range-breakout strategy: the opening-range box (09:30 + 09:45), the breakout entry, the +40-point target, and the stop at the far side of the box. |
| [`equity-curve.html`](equity-curve.html) | Account equity from $1,000 (10,000 units, 0.2-pip spread): target-40 across the full 5 years ($1,713) vs the walk-forward out-of-sample curve ($1,285), with the fold-by-fold table. |

Numbers behind these come from the backtest tools at the repo root
(`backtest.py`, `sweep.py`, `equity.py`, `walkforward.py`) — see [`../RESULTS.md`](../RESULTS.md).
