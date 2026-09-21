# BTC15 Backtest Report

> **Superseded.** This report gates on win rate after a flat fee buffer. Win rate is
> not a validation criterion for a binary contract: an 84% win rate bought at 90c
> loses money. `reports/VALIDATION_REPORT.md` measures gross edge (outcome minus
> entry price) over 68 days with fees excluded, and supersedes the numbers here.

Generated: 2026-09-20 UTC

## Directional validation — 180 days

The signal selects the side of the Kalshi target on which BTC is trading when five
minutes remain. The raw-confidence cutoff was selected on the first 60% of Binance
one-minute history, checked on the next 20%, and frozen for the final 20% holdout.

| Segment | Signals | Wins | Win rate | 95% lower bound | Max losing streak |
|---|---:|---:|---:|---:|---:|
| Train | 8,713 | 7,518 | 86.28% | 85.55% | 7 |
| Validation | 2,860 | 2,466 | 86.22% | 84.91% | 3 |
| Untouched holdout | 2,868 | 2,417 | 84.27% | 82.90% | 4 |

Result: directional accuracy cleared the requested 80% threshold on unseen data.

## Kalshi price validation — 30 days

Binance signals were joined to 2,847 settled `KXBTC15M` markets and their actual
five-minutes-remaining order-book candles.

| Rule | Signals | Win rate | Gross ROI | ROI after 2¢ buffer |
|---|---:|---:|---:|---:|
| Initial value filter | 694 | 67.00% | +0.38% | -2.62% |
| Optimized first half | 309 | 83.82% | +5.49% | +2.97% |
| Same rule, unseen second half | 354 | 79.66% | -0.29% | -2.79% |

Result: no fee-aware rule passed the untouched confirmation period. The optimized
rule failed out of sample, so the deployed strategy remains `enabled: false`.

## Flexible optimizer — exact Kalshi targets

The completed optimizer evaluated 14,229 historical decision snapshots from 2,847
settled markets. It tested:

- 3, 4, 5, 6, and 7 minutes remaining;
- raw model-confidence thresholds;
- minimum distance normalized by volatility;
- aligned and unrestricted momentum;
- multiple Kalshi entry-price bands;
- a 2¢ fee/slippage buffer.

One seven-minutes-remaining rule initially showed 84.69% holdout wins, but its
fee-buffered return was only +0.12%. Its 95% lower confidence bound did not exceed
the average entry cost plus the fee buffer, so the final robustness gate correctly
rejected it. No rule currently qualifies for live entry.

## Operational decision

The bot tracks the exact Kalshi open/close times, target, ticker, and Yes/No ask. It
sends `PAPER CANDIDATE` or `NO ENTRY` at the active rule's chosen time. The optimizer
runs daily and atomically updates `strategy.json`. It cannot label a trade `ENTRY ALERT`
until training, validation, untouched holdout, fees, and the economic confidence-bound
test all pass.

Limit orders must not remain open after the stated deadline. A limit that fills later
after the market moves against the signal is a different strategy and was not tested.

These results do not guarantee future performance. The 2¢ buffer is a conservative
testing assumption, not a statement of the user's exact account fee.

## Spike-reversion strategy — 30 days

The 12-to-10-minutes-remaining reversion test evaluated 8,533 snapshots from 2,847
settled markets. The fixed 30-35¢ entry and 50¢ take-profit rule produced 443 trades,
a 58.01% take-profit rate, and -16.90% return after the 2¢ buffer. No optimized rule
passed the train and validation profit gates. The strategy is implemented but remains
disabled for live entry. See `REVERSION_BACKTEST_REPORT.md` for the segment results.
