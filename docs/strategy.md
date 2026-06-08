# Strategy Notes

Quintava focuses on Polymarket-style 5-minute UP/DOWN markets.

## Market Model

Each 5-minute window has:

- a target price at the start of the window,
- a current BTC price during the window,
- a final outcome: `UP` if the window closes above the target, otherwise `DOWN`.

The bot only uses information available at entry time. This avoids look-ahead bias in both paper trading and backtesting.

## Current Research Defaults

```text
M5_STRATEGY=follow_lead
M5_ENTRY_MIN=1
M5_MIN_MOVE_PCT=0.05
M5_MIN_VOLATILITY_PCT=0.02
```

Interpretation:

- enter early in the 5-minute window,
- follow the current direction relative to the target price,
- require a minimum move before entering,
- skip low-volatility conditions.

## Odds and Payouts

Quintava estimates Polymarket-style odds from:

- distance from the target price,
- time left in the window,
- recent BTC volatility.

This produces variable paper payouts instead of flat even-money results. A winning trade returns `stake / entry_price`, while a losing trade loses the stake.

The odds model is transparent and useful for research, but it is not a live Polymarket quote.

## Research Warnings

- A high win rate can still be unprofitable if entry prices are too expensive.
- A larger payout usually means a higher probability of loss.
- Backtests should be validated out-of-sample.
- Live paper results should be collected over a meaningful sample before any real-money experiment is considered.
