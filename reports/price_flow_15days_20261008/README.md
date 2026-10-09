# Universal 85-cent price-flow replay — September 24 through October 8, 2026

This is a chronological, read-only replay of all 1,396 recorded primary BTC
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
| Wait >=85c only for two trades after a known loss | 415 | 338-77 | 81.45% | +$201.80 | -$80.21 | $181.03 | 13/15 |
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

Applying confirmation only for the next two trades after a known loss improved
the immediate-entry baseline, but it was weaker than universal confirmation.
With 1-cent worse entries it modeled +$21.26, an $187.37 maximum drawdown, and
12 target days. Universal confirmation under the same stress modeled +$111.79,
a $155.02 maximum drawdown, and 11 target days. At 5 cents worse, after-loss-only
modeled -$1,159.99 versus -$742.12 for universal confirmation.

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
| Oct 8 through 21:15 ET | 48 | 43 | 4 | +$21.43 | $36.71 | Yes |

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

## Maximum entry-price sweep

A strict maximum rejects quotes above the ceiling and continues waiting for
either side to return to the allowed 85c-to-ceiling band. This is distinct from
adding slippage to a hypothetical fill.

| Maximum entry | Trades | Win rate | Net | Worst day | Max DD | Target days |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 85c | 207 | 84.54% | -$77.43 | -$98.47 | $115.68 | 10/15 |
| 86c | 354 | 85.31% | -$69.69 | -$91.52 | $117.21 | 8/15 |
| 87c | 373 | 87.67% | +$113.00 | -$82.23 | $117.39 | 12/15 |
| 88c | 347 | 88.18% | +$103.71 | -$104.38 | $142.43 | 12/15 |
| 89c | 390 | 88.97% | +$154.44 | -$88.32 | $131.89 | 12/15 |
| 90c | 287 | 91.99% | +$343.66 | +$21.32 | $54.44 | 15/15 |
| 91c | 351 | 91.17% | +$328.00 | +$4.37 | $68.28 | 14/15 |
| 92c | 391 | 91.05% | +$328.61 | +$7.98 | $63.85 | 14/15 |
| 93c | 362 | 91.44% | +$325.77 | +$7.56 | $63.85 | 14/15 |
| 99c | 372 | 91.67% | +$326.03 | +$4.15 | $64.90 | 14/15 |

The strict 85c maximum is harmful in this recorded-quote replay because discrete
polling often first observes the crossing above 85c. A 90c ceiling is the best
point in this small, in-sample sweep, but that selection is optimized on the
same 15 days and needs forward fill evidence before production use.
