# BTC15 Spike-Reversion Backtest

> **Superseded.** This report gates on win rate after a flat fee buffer. Win rate is
> not a validation criterion for a binary contract: an 84% win rate bought at 90c
> loses money. `reports/VALIDATION_REPORT.md` measures gross edge (outcome minus
> entry price) over 68 days with fees excluded, and supersedes the numbers here.

Generated: 2026-09-20 UTC  
Period: 2026-08-21 to 2026-09-20 UTC

The test covered 2,847 settled KXBTC15M markets and 8,533 decision snapshots. It entered
the opposite contract at 30-35 cents after an early spike and rejection, then simulated
a 50-cent take-profit using subsequent executable bid candles. Positions whose take-profit
was not reached were settled using the recorded market result. Every trade includes a
conservative two-cent fee/slippage buffer.

| Segment | Trades | TP/profitable rate | 95% lower bound | Buffered P&L | Buffered ROI |
|---|---:|---:|---:|---:|---:|
| Train | 246 | 58.13% | 51.89% | -$13.17 | -16.51% |
| Validation | 79 | 54.43% | 43.50% | -$5.87 | -22.76% |
| Untouched holdout | 118 | 60.17% | 51.15% | -$5.29 | -13.77% |
| All | 443 | 58.01% | 53.37% | -$24.33 | -16.90% |

The optimizer tested spike size, rejection size, remaining distance, and take-profit
levels from 40 to 55 cents. No rule had sufficient positive train and validation
performance to qualify for the untouched holdout gate.

**Decision:** the strategy is implemented for paper alerts and continued testing, but
live entry remains disabled. Enabling it from this sample would be unsupported because
the actual 30-day fee-buffered result was negative.

Limitations: minute candles cannot prove queue position or fills; the two-cent buffer is
not an account-specific fee calculation; Binance and the settlement oracle can differ.
