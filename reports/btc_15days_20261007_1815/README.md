# BTC 15-day study through October 7, 2026, 18:15 New York

Current-rule counterfactual on real recorded signals, intrawindow polls, BRTI and settled outcomes. Uses the same fixed $995.93 daily opening as Claude's previous study; no compounding. $25 base, $30 for two taken trades after a known loss, 5 bps cushion, after-two-loss trend skip >10 bps, hold to settlement, stop at 8% ($79.6744). Fees included.

Coverage: 1,392 signals; 1,392 matched settled outcomes; zero dropped signal rows in the requested interval. October 7 is partial through the 18:15 close. Signals absent from the archive are not synthesized.

| Day | Signals | Taken | W?L | Modeled net | Target reached at close (ET) |
|---|---:|---:|---:|---:|---|
| 09-23 | 96 | 13 | 12?1 | $+82.98 | 03:15 |
| 09-24 | 87 | 46 | 36?10 | $+83.21 | 13:30 |
| 09-25 | 95 | 17 | 15?2 | $+80.06 | 04:15 |
| 09-26 | 96 | 68 | 52?16 | $+6.85 | Not reached |
| 09-27 | 96 | 32 | 27?5 | $+84.86 | 08:15 |
| 09-28 | 96 | 70 | 53?17 | $+84.74 | 18:00 |
| 09-29 | 96 | 25 | 21?4 | $+84.43 | 06:15 |
| 09-30 | 96 | 13 | 12?1 | $+84.74 | 03:15 |
| 10-01 | 81 | 38 | 32?6 | $+80.84 | 13:15 |
| 10-02 | 96 | 21 | 18?3 | $+82.64 | 05:15 |
| 10-03 | 96 | 63 | 50?13 | $+55.03 | Not reached |
| 10-04 | 96 | 78 | 59?19 | $-28.40 | Not reached |
| 10-05 | 96 | 95 | 72?23 | $-36.31 | Not reached |
| 10-06 | 96 | 86 | 60?26 | $-231.58 | Not reached |
| 10-07 | 73 | 17 | 15?2 | $+84.63 | 04:15 |

Total $+598.72; 682 entries, 534 wins / 148 losses (78.3%); 10 target days, 3 losing days.

Validation: every daily total matches an independent call to Claude's original new_setup.py accounting within $0.0001, with the same cutoff. Both methods share the original reference data loader; this is an arithmetic/regression check, not independent validation of fill assumptions.

## Important limits
- Real recorded signals, polls and outcomes; hypothetical fills at observed asks.
- No order-book execution, retry or chase fill simulation.
- Fixed 995.93 daily opening, no compounding or deposit reconstruction.
- Previous outcome assumed known before next signal, as in original Claude study; delayed result availability not reconstructed.
- Current-rule counterfactual, not historical account profit.

The test does NOT reproduce the full live order lifecycle, slippage, partial fills, failed orders or the 93-cent chase. Observed asks are not guaranteed executions. Earlier outcome availability may lag the timestamps assumed by the old study. It is not a faithful execution simulator.

Actual lifecycle statuses are attached to modeled signal rows in trades.csv. historical_recorded_lifecycle.json separately reports available historical filled-row P&L under the historical stakes/rules/exits; it is not the current-rule counterfactual and is not a freshly reconciled broker ledger. Dedicated all-signal history begins September 28.

No production source/settings/database writes, network trading calls, or service restarts. New files only in this research directory.