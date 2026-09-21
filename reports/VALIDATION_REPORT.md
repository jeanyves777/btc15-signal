# BTC15 Strategy Validation - Gross Edge

Generated: 2026-09-21T00:24:05.862703+00:00
Data: 6435 settled markets, 77220 decision snapshots
Period: 2026-07-14T23:45:00+00:00 to 2026-09-20T23:00:00+00:00

Fees are excluded from every pass/fail gate below. The question is whether the
strategy beats the price it pays, before costs. Fee-adjusted figures appear
alongside for information only.

## Edge benchmark - indiscriminate trading on the same markets

| Baseline | Trades | Win rate | Edge/contract | Gross ROI | 95% CI on edge |
|---|---:|---:|---:|---:|---:|
| always yes | 75791 | 49.24% | $-0.0034 | -0.68% | $-0.0093 to $0.0028 |
| always favourite | 74582 | 78.90% | $0.0047 | 0.60% | $-0.0000 to $0.0094 |
| coin flip | 75841 | 49.10% | $-0.0045 | -0.91% | $-0.0068 to $-0.0018 |

A calibrated market prices every one of these at zero edge. They are the bar.

## Primary strategy - POSITIVE_BUT_INCONCLUSIVE

| Measure | Trades | Win rate | Edge/contract | Gross ROI | 95% CI on edge |
|---|---:|---:|---:|---:|---:|
| Deployed rule, full sample | 1674 | 74.49% | $0.0083 | 1.13% | $-0.0115 to $0.0272 |
| Walk-forward out of sample | 583 | 75.99% | $0.0059 | 0.78% | $-0.0241 to $0.0379 |

Best rule the full grid found scores $0.0481 of edge per contract. Shuffling outcomes within price buckets and rerunning the whole search produces a median best of $0.0822 and a 95th percentile of $0.1227, giving **p = 0.990** over 300 permutations.

### Trigger to close - was the loss ever a win?

Of 140 losing trades, **102 (72.9%)** could have been closed for at least 2c of profit at some point before expiry. Median best price a loser reached was $0.0750 above entry.

Of 443 winners, 355 (80.1%) were at least 2c underwater on the way, so a tight stop would have cut them.

| Profit reachable | Losers that got there | Share of losers | Median minute |
|---|---:|---:|---:|
| $0.01 | 113 | 80.7% | 1.0 |
| $0.02 | 102 | 72.9% | 1.0 |
| $0.03 | 97 | 69.3% | 1.0 |
| $0.05 | 82 | 58.6% | 1.0 |
| $0.08 | 69 | 49.3% | 2.0 |
| $0.10 | 62 | 44.3% | 2.0 |
| $0.15 | 40 | 28.6% | 3.0 |
| $0.20 | 23 | 16.4% | 4.0 |
| $0.25 | 15 | 10.7% | 5.0 |

### Exit policy on identical trades

Each policy is compared to holding the *same* trades to expiry, paired trade by trade. `vs hold` is the average dollars per contract gained or lost by switching, with a 95% interval; a policy only beats holding if that interval clears zero.

| Policy | Win rate | Gross ROI | vs hold / contract | 95% CI on vs hold |
|---|---:|---:|---:|---:|
| sl 10c | 39.45% | 3.20% | +0.0182 | -0.0079 to +0.0461 |
| trail 8c | 45.63% | 2.47% | +0.0127 | -0.0146 to +0.0423 |
| tp 15c sl 10c | 47.17% | 1.43% | +0.0048 | -0.0230 to +0.0360 |
| hold to expiry | 75.99% | 0.78% | +0.0000 | +0.0000 to +0.0000 |
| tp 10c sl 10c | 53.17% | 0.56% | -0.0017 | -0.0284 to +0.0299 |
| exit minute 3 | 64.84% | -0.83% | -0.0122 | -0.0376 to +0.0149 |
| exit minute 5 | 68.10% | -1.07% | -0.0140 | -0.0362 to +0.0111 |
| tp 15c | 82.85% | -1.53% | -0.0174 | -0.0345 to +0.0019 |
| tp 10c | 86.62% | -2.00% | -0.0210 | -0.0430 to +0.0031 |
| tp 5c | 90.05% | -3.72% | -0.0340 | -0.0585 to -0.0073 |

10 policies were compared on the same trades, so the best row is partly a selection artefact even before the interval is read. No policy's interval clears zero, so none is demonstrably better than simply holding to expiry on this sample.

High win rate is not the same as profit here: a tight take-profit wins nearly every trade and still loses money, because the occasional full loss outweighs many small gains. That is the same trap the earlier win-rate-based reports fell into.

### Named rules, no search behind them

A grid of thousands needs a permutation test because the search itself can
manufacture an edge. These rules were fixed in advance, so they need only an
honest interval and a correction across this short list. Each is split in half
chronologically - an effect in one half and not the other is a regime, not an
edge. They were chosen after inspecting the full-sample calibration curve, so
the two halves are the real evidence, not the total.

| Rule | Trades | Win rate | Avg price | Edge/contract | 95% CI | 1st half | 2nd half | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| favourite 10min | 843 | 91.46% | 0.8947 | **+0.0199** | +0.0008 to +0.0389 | +0.0225 | +0.0170 | noise |
| favourite 9min | 1221 | 91.89% | 0.9025 | **+0.0164** | +0.0016 to +0.0300 | +0.0068 | +0.0260 | favourable |
| favourite 7min | 1920 | 95.00% | 0.9212 | **+0.0288** | +0.0192 to +0.0380 | +0.0261 | +0.0311 | favourable |
| favourite 5min | 2493 | 95.19% | 0.9406 | **+0.0113** | +0.0030 to +0.0197 | +0.0126 | +0.0103 | favourable |
| deep favourite 10min | 408 | 93.38% | 0.9222 | **+0.0116** | -0.0106 to +0.0337 | +0.0073 | +0.0183 | noise |
| confident far 10min | 3813 | 70.81% | 0.6964 | **+0.0117** | -0.0013 to +0.0260 | +0.0126 | +0.0107 | noise |
| longshot 10min | 80 | 20.00% | 0.2510 | -0.0510 | -0.1105 to +0.0095 | -0.0483 | -0.2600 | noise |

Bold marks a rule positive in *both* halves. A rule that is significant overall but positive in only one half has not shown a durable edge.

### Regime findings (10% FDR across all buckets tested; out-of-sample walk-forward trades)

No regime survived correction (18 buckets tested). Every apparent pocket of performance is consistent with noise.

## Reversion strategy - NO_GROSS_EDGE

| Measure | Trades | Win rate | Edge/contract | Gross ROI | 95% CI on edge |
|---|---:|---:|---:|---:|---:|
| Deployed rule, full sample | 760 | 31.32% | $-0.0440 | -13.52% | $-0.0609 to $-0.0277 |
| Walk-forward out of sample | 744 | 42.47% | $-0.0445 | -10.08% | $-0.0624 to $-0.0262 |

### Trigger to close - was the loss ever a win?

Of 428 losing trades, **332 (77.6%)** could have been closed for at least 2c of profit at some point before expiry. Median best price a loser reached was $0.1200 above entry.

Of 316 winners, 291 (92.1%) were at least 2c underwater on the way, so a tight stop would have cut them.

| Profit reachable | Losers that got there | Share of losers | Median minute |
|---|---:|---:|---:|
| $0.01 | 352 | 82.2% | 1.0 |
| $0.02 | 332 | 77.6% | 1.0 |
| $0.03 | 317 | 74.1% | 1.0 |
| $0.05 | 286 | 66.8% | 1.0 |
| $0.08 | 255 | 59.6% | 2.0 |
| $0.10 | 223 | 52.1% | 2.0 |
| $0.15 | 185 | 43.2% | 3.0 |
| $0.20 | 149 | 34.8% | 4.0 |
| $0.25 | 124 | 29.0% | 5.0 |

### Exit policy on identical trades

Each policy is compared to holding the *same* trades to expiry, paired trade by trade. `vs hold` is the average dollars per contract gained or lost by switching, with a 95% interval; a policy only beats holding if that interval clears zero.

| Policy | Win rate | Gross ROI | vs hold / contract | 95% CI on vs hold |
|---|---:|---:|---:|---:|
| trail 8c | 32.39% | -0.50% | +0.0143 | -0.0184 to +0.0467 |
| sl 10c | 14.78% | -1.24% | +0.0110 | -0.0187 to +0.0387 |
| exit minute 5 | 44.09% | -3.62% | +0.0005 | -0.0247 to +0.0268 |
| hold to expiry | 42.47% | -3.74% | +0.0000 | +0.0000 to +0.0000 |
| tp 15c sl 10c | 31.99% | -4.54% | -0.0035 | -0.0351 to +0.0296 |
| exit minute 3 | 41.80% | -5.26% | -0.0067 | -0.0372 to +0.0211 |
| tp 10c sl 10c | 37.50% | -5.67% | -0.0085 | -0.0405 to +0.0248 |
| tp 15c | 67.34% | -8.58% | -0.0213 | -0.0467 to +0.0046 |
| tp 5c | 80.91% | -8.96% | -0.0230 | -0.0544 to +0.0063 |
| tp 10c | 72.45% | -9.94% | -0.0274 | -0.0549 to +0.0005 |

10 policies were compared on the same trades, so the best row is partly a selection artefact even before the interval is read. No policy's interval clears zero, so none is demonstrably better than simply holding to expiry on this sample.

High win rate is not the same as profit here: a tight take-profit wins nearly every trade and still loses money, because the occasional full loss outweighs many small gains. That is the same trap the earlier win-rate-based reports fell into.

### Regime findings (10% FDR across all buckets tested; out-of-sample walk-forward trades)

| Dimension | Bucket | Trades | Win rate | Edge/contract | p | Verdict |
|---|---|---:|---:|---:|---:|---|
| session | asia | 226 | 41.59% | $-0.0544 | 0.0033 | adverse |
| session | us | 289 | 39.10% | $-0.0521 | 0.0017 | adverse |
| vol_regime | high | 304 | 41.78% | $-0.0534 | 0.0017 | adverse |
| vol_regime | low | 160 | 40.62% | $-0.0743 | 0.0017 | adverse |
| trend_regime | high | 292 | 41.10% | $-0.0523 | 0.0017 | adverse |
| trend_regime | low | 212 | 39.15% | $-0.0382 | 0.0233 | adverse |
| trend_regime | mid | 240 | 47.08% | $-0.0405 | 0.0133 | adverse |
| liquidity_regime | high | 300 | 38.33% | $-0.0487 | 0.0017 | adverse |
| liquidity_regime | low | 194 | 47.94% | $-0.0315 | 0.0767 | adverse |
| liquidity_regime | mid | 250 | 43.20% | $-0.0494 | 0.0017 | adverse |
| distance_regime | atm | 250 | 40.00% | $-0.0512 | 0.0033 | adverse |
| distance_regime | near | 494 | 43.72% | $-0.0411 | 0.0017 | adverse |
| side | DOWN | 607 | 44.81% | $-0.0388 | 0.0017 | adverse |
| side | UP | 137 | 32.12% | $-0.0697 | 0.0033 | adverse |

## What these numbers cannot show

- Minute candles cannot prove queue position. A displayed bid is not a guaranteed fill.
- Take-profit fills assume the period's best quote was reachable, which flatters every
  early-exit policy in the table above.
- Settlement uses CF Benchmarks BRTI averaged over the final 60 seconds, not the Binance
  close the features are built from. That basis is a real source of error, measured in
  the snapshot table as `oracle_basis_bps`.
- The permutation test shuffles outcomes independently within price buckets, which
  removes the serial correlation between neighbouring windows. That makes the null
  slightly tighter than reality, so its p-values are, if anything, generous to the
  strategy rather than harsh on it.
- Gross edge is a necessary condition for a live strategy, not a sufficient one. A
  strategy that clears this bar still has to clear fees and slippage afterwards.

