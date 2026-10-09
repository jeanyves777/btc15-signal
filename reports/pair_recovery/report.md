# BTC loss-triggered BTC/GOLD recovery: fixed-rule backtest

Data: Local export 68fc43d (--days 14, observation-derived outcomes). TESTING ONLY - no strategy change, no orders.

## Fixed rule under test

- Budget $25.00 per paired entry, shared; qty = floor(25 / (BTC ask + GOLD ask)), same on both legs; unused budget kept as cash.
- Decision exactly 8 min before the shared 15-min expiry, latest quote captured at or before that instant, stale if older than 30s.
- Combined selected-side executable ask <= $0.50.
- BTC UP on the first window of each New York day, alternating per window; GOLD opposite.
- Held to settlement. Leg P&L = payout - cost; Kalshi fees are NOT deducted from recovery legs (rule as specified); the estimate is shown separately.

## Headline (full period)

| | A. BTC alone (recorded) | B. BTC + recovery |
|---|---:|---:|
| Net P&L | +174.79 | +341.72 |
| - of which normal BTC | +174.79 | +30.07 |
| - of which recovery | - | +311.65 (BTC legs +128.26, GOLD legs +183.39) |
| BTC wins / losses | 486 / 112 | 63 / 9 |
| Max drawdown | 269.20 | 30.43 |
| Peak capital required (incl. unsettled) | 14.64 | 23.67 |

Peak capital = the most cash ever out at once: realised losses to date plus the stake in every position not yet paid (payment time, not outcome time).

Recovery fees not deducted (estimate at Kalshi taker schedule): $65.41. B net after that estimate: +276.31.

Recorded BTC trades suppressed by recovery in B: 526 (their recorded net P&L: +144.72).

## Recovery activity

- Recovery windows: 1062; paired entries: 49; unmatched-only entries: 0; skipped: 1013 (of which awaiting activation: 0).
- Both win 0, both lose 0, BTC-only win 3, GOLD-only win 5, unresolved 41.
- Cycles completed 8, unfinished 1. Longest recovery: cycle 6, 75.0 h over 300 windows.
- Fill basis: {'quote_based_unverified': 49}. Unmatched-leg contracts: 0 (their P&L inside recovery: +0.00).

### Skipped windows by condition

| Condition | First failing | Any failing |
|---|---:|---:|
| btc_quote_missing | 23 | 23 |
| gold_quote_missing | 477 | 500 |
| btc_quote_stale | 1 | 2 |
| gold_quote_stale | 0 | 1 |
| btc_ask_unavailable | 0 | 0 |
| gold_ask_unavailable | 0 | 0 |
| combined_ask_above_ceiling | 512 | 513 |
| zero_contracts | 0 | 0 |
| no_depth_at_ask | 0 | 0 |

## Cycles

| # | Trigger | Loss | Recovery start | Paired | Skipped | Windows | Duration (min) | Result |
|---|---|---:|---|---:|---:|---:|---:|---|
| 1 | KXBTC15M-26SEP232015-15 | 1.60 | 2026-09-23 20:30:00 | 1 | 121 | 122 | 1830 | surplus +33.80 |
| 2 | KXBTC15M-26SEP260030-30 | 0.87 | 2026-09-26 00:45:00 | 2 | 176 | 178 | 2670 | surplus +63.30 |
| 3 | KXBTC15M-26SEP280315-15 | 3.24 | 2026-09-28 03:30:00 | 1 | 0 | 1 | 15 | surplus +23.80 |
| 4 | KXBTC15M-26SEP281915-15 | 0.82 | 2026-09-28 19:30:00 | 10 | 127 | 137 | 2055 | surplus +39.48 |
| 5 | KXBTC15M-26SEP301300-00 | 2.86 | 2026-09-30 13:15:00 | 9 | 138 | 147 | 2205 | surplus +39.35 |
| 6 | KXBTC15M-26OCT020215-15 | 6.43 | 2026-10-02 02:30:00 | 12 | 288 | 300 | 4500 | surplus +32.61 |
| 7 | KXBTC15M-26OCT050930-30 | 23.97 | 2026-10-05 09:45:00 | 11 | 124 | 135 | 2025 | surplus +4.12 |
| 8 | KXBTC15M-26OCT062000-00 | 30.43 | 2026-10-06 20:15:00 | 3 | 26 | 29 | 435 | surplus +4.97 |
| 9 | KXBTC15M-26OCT071215-15 | 0.45 | 2026-10-07 12:30:00 | 0 | 13 | 13 | 195 | UNFINISHED, debt -0.45 |

## Daily net P&L (New York day of outcome)

| Day | A. BTC alone | B. normal BTC | B. recovery | B. total |
|---|---:|---:|---:|---:|
| 2026-09-23 | -3.05 | +0.93 | +0.00 | +0.93 |
| 2026-09-24 | -3.89 | +0.00 | +0.00 | +0.00 |
| 2026-09-25 | +2.34 | +1.95 | +35.40 | +37.35 |
| 2026-09-26 | -0.96 | -0.77 | +0.00 | -0.77 |
| 2026-09-27 | +6.57 | +0.85 | +64.17 | +65.02 |
| 2026-09-28 | +3.28 | +3.57 | +27.04 | +30.61 |
| 2026-09-29 | +1.59 | +0.00 | +0.00 | +0.00 |
| 2026-09-30 | +9.02 | -2.86 | +40.30 | +37.44 |
| 2026-10-01 | +31.95 | +0.00 | +0.00 | +0.00 |
| 2026-10-02 | +2.31 | -4.18 | +42.21 | +38.03 |
| 2026-10-03 | +116.09 | +0.00 | +0.00 | +0.00 |
| 2026-10-04 | +156.65 | +0.00 | +0.00 | +0.00 |
| 2026-10-05 | -10.80 | +60.52 | +39.04 | +99.56 |
| 2026-10-06 | -169.99 | -30.43 | +28.09 | -2.34 |
| 2026-10-07 | +33.69 | +0.50 | +35.40 | +35.90 |
| **Total** | +174.79 | +30.07 | +311.65 | +341.72 |

## Reconciliation examples (mixed results first)

**2026-09-27 21:00:00 - btc_only_win** (cycle 2, quote_based_unverified)
- BTC DOWN KXBTC15M-26SEP272100-00: quote 2026-09-27 20:51:58 no_ask=0.02; 89 x 0.02 = cost 1.69; result no -> WIN; payout 89.00; P&L 89.00 - 1.69 = +87.31
- GOLD UP KXGOLD15M-26SEP272100-00: quote 2026-09-27 20:51:50 yes_ask=0.26; 89 x 0.26 = cost 23.14; result no -> LOSS; payout 0.00; P&L 0.00 - 23.14 = -23.14
- Combined: ask 0.28, cost 24.83, unused cash 0.17, payout 89.00, P&L +87.31 + -23.14 = +64.17; recovery balance -0.87 -> +63.30

**2026-09-28 03:30:00 - btc_only_win** (cycle 3, quote_based_unverified)
- BTC DOWN KXBTC15M-26SEP280330-30: quote 2026-09-28 03:21:48 no_ask=0.28; 52 x 0.28 = cost 14.56; result no -> WIN; payout 52.00; P&L 52.00 - 14.56 = +37.44
- GOLD UP KXGOLD15M-26SEP280330-30: quote 2026-09-28 03:21:52 yes_ask=0.20; 52 x 0.20 = cost 10.40; result no -> LOSS; payout 0.00; P&L 0.00 - 10.40 = -10.40
- Combined: ask 0.48, cost 24.96, unused cash 0.04, payout 52.00, P&L +37.44 + -10.40 = +27.04; recovery balance -3.24 -> +23.80

**2026-09-25 02:45:00 - gold_only_win** (cycle 1, quote_based_unverified)
- BTC UP KXBTC15M-26SEP250245-45: quote 2026-09-25 02:36:59 yes_ask=0.12; 60 x 0.12 = cost 7.20; result no -> LOSS; payout 0.00; P&L 0.00 - 7.20 = -7.20
- GOLD DOWN KXGOLD15M-26SEP250245-45: quote 2026-09-25 02:36:53 no_ask=0.29; 60 x 0.29 = cost 17.40; result no -> WIN; payout 60.00; P&L 60.00 - 17.40 = +42.60
- Combined: ask 0.41, cost 24.60, unused cash 0.40, payout 60.00, P&L -7.20 + +42.60 = +35.40; recovery balance -1.60 -> +33.80

**2026-10-02 01:45:00 - gold_only_win** (cycle 5, quote_based_unverified)
- BTC UP KXBTC15M-26OCT020145-45: quote 2026-10-02 01:36:51 yes_ask=0.11; 67 x 0.11 = cost 7.37; result no -> LOSS; payout 0.00; P&L 0.00 - 7.37 = -7.37
- GOLD DOWN KXGOLD15M-26OCT020145-45: quote 2026-10-02 01:36:59 no_ask=0.26; 67 x 0.26 = cost 17.42; result no -> WIN; payout 67.00; P&L 67.00 - 17.42 = +49.58
- Combined: ask 0.37, cost 24.79, unused cash 0.21, payout 67.00, P&L -7.37 + +49.58 = +42.21; recovery balance -2.86 -> +39.35

## Data coverage

- windows_in_period: 1345
- BTC_windows_with_quotes: 1322
- BTC_windows_with_outcome: 513
- BTC_quote_rows: 95477
- BTC_quote_rows_with_depth: 0
- GOLD_windows_with_quotes: 795
- GOLD_windows_with_outcome: 1165
- GOLD_quote_rows: 62068
- GOLD_quote_rows_with_depth: 0
- windows_missing_a_quote_series: 550
- first_window_close: 2026-09-23 15:30:00
- last_window_close: 2026-10-07 15:30:00

## Validation

- Entry decisions read only quotes captured at or before close - 8 min (checked per row below).
- Outcomes resolve only positions already taken; recovery state changes at the outcome time, and the next window is the first that can see it.
- A is the actual recorded BTC replay. B is a counterfactual: its normal-mode BTC trades are replayed from the FIXED ARCHIVE (not regenerated); recorded trades inside recovery windows are suppressed. The live system's own state (its recovery sizing, daily limits) shaped the archive and is not re-simulated.
- Recovery fills: depth-verified when the ask-level size was recorded for both legs, otherwise quote-based with unverified fills. Never walks the book.
- Automated reconciliation: ALL CHECKS PASSED
- No parameter was optimised: every threshold above is the fixed rule.
