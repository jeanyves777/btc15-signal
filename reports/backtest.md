# BTC15 Strategy Backtest

> **Superseded.** This report gates on win rate after a flat fee buffer. Win rate is
> not a validation criterion for a binary contract: an 84% win rate bought at 90c
> loses money. `reports/VALIDATION_REPORT.md` measures gross edge (outcome minus
> entry price) over 68 days with fees excluded, and supersedes the numbers here.

Generated: 2026-09-20T21:04:13.861578+00:00
Period: 2026-03-24 to 2026-09-20 UTC
Selected raw-probability threshold: **50%**

| Segment | Signals | Wins | Win rate | 95% lower bound | Max losing streak |
|---|---:|---:|---:|---:|---:|
| Train | 8713 | 7518 | 86.28% | 85.55% | 7 |
| Validation | 2860 | 2466 | 86.22% | 84.91% | 3 |
| Holdout | 2868 | 2417 | 84.27% | 82.90% | 4 |

## Interpretation

A strategy is not validated at the requested level unless the untouched holdout has enough signals and its 95% lower confidence bound is at least 80%.

## Limitations

- Historical top-of-book depth and futures basis are unavailable in kline data.
- Results measure direction accuracy, not profit; contract prices and fees are absent.
- A Binance close may differ from the contract settlement oracle.
