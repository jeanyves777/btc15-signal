# Universal 85-cent price-flow replay — September 24 through October 8, 2026

This is a chronological, read-only replay of all 1,395 recorded primary BTC
signals across 15 calendar days. For the candidate rule, each signal waits until
the original side or the opposite side first shows an ask of at least 85 cents.
The original side wins an exact-timestamp tie. The trade must qualify with at
least 120 seconds remaining. The rule is also applied after known losses.

The current-policy comparison uses a fixed $708.75 daily opening for consistent
sizing, a $25 base entry, $30 for the next two taken trades after a known loss,
Kalshi fees, hold to settlement, and a latched 3% daily target of $21.26. It does
not compound between days.

## Main comparison

| Rule | Trades | W-L | Win rate | Net | Worst day | Max realized DD | Target days |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Enter every signal at its observed ask | 471 | 347-124 | 73.67% | -$151.79 | -$428.41 | $463.38 | 13/15 |
| Wait for either side >=85c | 372 | 341-31 | 91.67% | +$326.03 | +$4.15 | $64.90 | 14/15 |
| Wait for either side >=85c, add 1c | 578 | 522-56 | 90.31% | +$111.79 | -$105.17 | $155.02 | 11/15 |
| Wait for either side >=85c, add 5c | 755 | 678-77 | 89.80% | -$743.86 | -$186.80 | $213.26 | 7/15 |

The number of trades changes under execution stress because worse modeled entry
prices change when the daily target is reached. At observed asks, 55 entries
flipped from the generated direction; 54 won and their modeled net was +$141.46.
The remaining 317 entries went in the generated direction; 287 won and netted
+$184.57. Before target pauses, neither side reached 85 cents in 60 windows.

Without the daily target, the candidate took 1,215 of 1,395 signals, flipped 174,
won 89.22%, and modeled +$173.10 at observed asks. The immediate-entry baseline
won 74.34% and modeled -$209.51. Neither side reached 85 cents before cutoff in
180 windows.

## Daily observed-ask result with the 3% target

| Day | Trades | W | Flips | Net | Max realized DD | Target |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Sep 24 | 74 | 66 | 9 | +$4.15 | $64.90 | No |
| Sep 25 | 8 | 8 | 1 | +$24.62 | $0.00 | Yes |
| Sep 26 | 50 | 45 | 10 | +$23.75 | $60.56 | Yes |
| Sep 27 | 8 | 8 | 1 | +$24.02 | $0.00 | Yes |
| Sep 28 | 25 | 23 | 7 | +$21.68 | $29.25 | Yes |
| Sep 29 | 16 | 15 | 2 | +$22.76 | $24.91 | Yes |
| Sep 30 | 15 | 14 | 0 | +$22.40 | $25.11 | Yes |
| Oct 1 | 7 | 7 | 1 | +$23.36 | $0.00 | Yes |
| Oct 2 | 7 | 7 | 1 | +$23.70 | $0.00 | Yes |
| Oct 3 | 7 | 7 | 1 | +$22.98 | $0.00 | Yes |
| Oct 4 | 16 | 15 | 2 | +$23.76 | $24.91 | Yes |
| Oct 5 | 34 | 31 | 5 | +$22.83 | $50.76 | Yes |
| Oct 6 | 8 | 8 | 2 | +$23.09 | $0.00 | Yes |
| Oct 7 | 49 | 44 | 9 | +$21.52 | $48.38 | Yes |
| Oct 8 through 21:00 ET | 48 | 43 | 4 | +$21.43 | $36.71 | Yes |

## Limits

These results use real recorded signals, outcomes, and quote lifecycle samples,
but observed asks are not executable fills and do not prove available depth.
The zero-cent case is optimistic. The one-cent and five-cent rows show that the
apparent advantage is highly sensitive to execution price; the full configured
five-cent limit allowance makes this replay unprofitable. Polling is discrete,
so the true first market crossing can occur between observations. This evidence
supports continued testing and shadow measurement, but it does not establish the
observed-ask result as achievable live profit.

Reproduce with `python reports/price_flow_15days_20261008/run.py`. Detailed
outputs are in `results.json`, `daily.csv`, and `trades.csv`.
