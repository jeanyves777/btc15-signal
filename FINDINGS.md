# Measured findings

Every number here came from the historical dataset (`data/market_data.db`,
~6,400 settled KXBTC15M markets) or from live trading. Each is stated with its
sample size and interval so a later session can tell evidence from opinion.

**Read this before changing the strategy.** Several entries exist because a
plausible change was made on reasoning alone and the measurement contradicted
it. Two of those changes were mine, made the same day.

Confidence intervals are 95%, from a **moving-block bootstrap clustered by
market**, because consecutive minutes inside one 15-minute window are not
independent observations and treating them as such roughly halves the interval.

---

## 1. The edge is real, but fees decide where it lives

Buying the **favourite** (the dearer side) at 6-11 minutes remaining, held to
settlement. Entries drawn from every qualifying minute, clustered by market.

| Band | n | Gross edge | **Net of fees** | 95% CI (net) | p |
|---|---|---|---|---|---|
| 0.70-0.99 | 22,560 | +0.0166 | +0.0072 | **[-0.0009, +0.0154]** | 0.045 |
| 0.75-0.99 | 18,367 | +0.0169 | +0.0087 | [-0.0005, +0.0172] | 0.029 |
| 0.80-0.99 | 14,190 | +0.0164 | **+0.0094** | [+0.0009, +0.0177] | 0.017 |
| 0.85-0.99 | 10,157 | +0.0176 | **+0.0119** | [+0.0040, +0.0205] | 0.003 |
| 0.88-0.99 | 7,777 | +0.0138 | +0.0090 | [+0.0009, +0.0178] | 0.017 |
| 0.90-0.99 | 6,309 | +0.0126 | +0.0084 | [-0.0001, +0.0165] | 0.026 |

**The fee is `0.07 × P × (1-P)`, so it PEAKS mid-book and vanishes at the
extremes.** The cheap end of the band is the expensive end after costs:

| Entry | Fee | Edge after fee |
|---|---|---|
| 0.70 | 0.0147 | +0.0019 (89% of the edge eaten) |
| 0.80 | 0.0112 | +0.0054 |
| 0.90 | 0.0063 | +0.0103 |
| 0.99 | 0.0007 | +0.0159 (96% survives) |

**Mistake to avoid:** on 2026-09-21 the band was widened to 0.70 on *gross*
edge, to get more trades. Net of fees that band's interval straddles zero. It
was corrected to 0.80 the same day. **Always measure net.**

**Deployed:** `min_ask 0.80`, `max_ask 0.99`.

---

## 2. Early exits lose money. All of them.

5,753 **paired** trades - identical entries, exit rule vs hold to settlement.

### Stop-loss (sell when BTC crosses back through the strike)

| | |
|---|---|
| Exit minus hold | **-$0.0162/contract**, CI [-0.0233, -0.0089] |
| How often it fired | **50% of all trades** |
| How often it was wrong | **79% of the times it fired** |
| Average damage when it fired | -$0.0325/contract |

Against a gross entry edge of ~+$0.017, it destroyed the entire edge.

Every variant was tested and every one lost to holding:

| Variant | vs hold |
|---|---|
| any cross (the original) | -0.0162 |
| cross held 2 minutes | -0.0153 |
| cross held 3 minutes | -0.0124 |
| cross by 5 / 10 / 20 / 40 bps | -0.0162 (identical - crosses gap well past) |
| only while bid >= 0.70 | -0.0147 |
| only while bid >= 0.55 | -0.0151 |
| bid >= 0.70 AND held 3 min | -0.0110 (best, still negative) |

### Take-profit (cash out once the profit is in)

| Rule | vs hold |
|---|---|
| bid >= 0.90 | -0.0127 |
| bid >= 0.95 | -0.0075 |
| bid >= 0.98 | -0.0018 |
| **bank 90% of available profit** | **-0.0006** (noise) |

**Why none of them can work.** The contract price IS the market's probability,
so by optional stopping any exit is EV-neutral *before* costs - the same
argument `validation.py` uses to set zero as the null. An exit can therefore
only ever pay the spread a second time plus a second fee. What it buys is lower
variance, which is worth nothing at $1 a trade.

**Deployed:** stop-loss **off**. Cash-out **on** at 90% of available profit,
because at that threshold the cost is inside the noise and it does bank the
money - chosen for certainty, not for profit.

**Correction:** an earlier claim in this repo that the reversal exit "raised net
profit from $140 to $216 and halved drawdown" came from an older profit test
with a different entry rule. **It does not survive this measurement.**

---

## 3. The alert lock was the biggest limiter in the system

The Telegram alert and the trading decision shared one gate. The alert fires at
the first poll above the manual floor - usually while the price is still walking
up through the 60s and 70s - and that locked the window for the rest of its life.

Across 6,428 windows:

| | Windows | |
|---|---|---|
| Qualified at or before the alert fires | 1,454 | 23% |
| **Qualified only after the window locked** | **2,941** | **46%** |
| Never qualified | 2,033 | 32% |

**67% of every window that ever qualified was unreachable.** Separating alerting
(once per window) from trading (every poll) multiplies tradeable windows by ~3x.

This mattered far more than any band adjustment.

---

## 4. Session and time of day: track it, do not trade on it

0.70-0.99 band, 6-11 minutes, gross, clustered by market.

| Session (UTC) | n | Edge | 95% CI |
|---|---|---|---|
| Asia 00-07 | 7,356 | +0.0093 | **[-0.0075, +0.0247]** |
| London 08-12 | 4,717 | +0.0213 | [+0.0016, +0.0383] |
| US open 13-16 | 3,982 | +0.0223 | [+0.0019, +0.0417] |
| US pm 17-20 | 3,784 | +0.0116 | **[-0.0096, +0.0334]** |
| Late 21-23 | 2,721 | +0.0266 | [+0.0001, +0.0504] |

Every session is positive on the point estimate, two straddle zero, and **every
interval overlaps every other**. Filtering on this would be fitting the sample,
not the market - and Asia alone is a third of all opportunity. A test fails if
session data ever reaches the trade-decision path.

---

## 5. Fees, measured against real fills

Kalshi charges `ceil(0.07 × C × P × (1-P) × 10⁴) / 10⁴` - **ceiling to the
hundredth of a cent** - and only *displays* it rounded to cents.

| Fill | Raw | Charged |
|---|---|---|
| 1 contract @ 0.84 | 0.9408c | **$0.0095** |
| 13.5 contracts @ 0.74 | 18.1818c | ticket showed $0.18 |

Cent-flooring gives $0.00 on the first, cent-ceiling gives $0.01. Only 4dp
ceiling fits both. At one contract the difference IS the whole fee, and about a
tenth of the expected profit on a $1 trade.

Use `kalshi_fee_charged`. `kalshi_fee_observed` (cent-floored) is kept only so
older backtests stay comparable with each other.

---

## 6. Live execution

- **Orders posted at exactly the touch missed 40% of the time.** An IOC only
  fills if the resting size survives the round trip. `entry_slippage = $0.01` is
  a ceiling, not a cost: a limit still fills at the best available price, so it
  is paid only when the book actually moved.
- **The fills feed lags the order.** An immediate lookup finds nothing; the fill
  appears seconds later. Observed live: limit 74c, fill 73c, invisible at first
  ask. `record_fill_detail` retries 4 times with backoff.
- **Books gap, they do not drift.** On 2026-09-21 the 08:45 book went
  0.76 → 0.85 → 0.87 → 0.92 in under a minute and settled at 96%. No slippage
  setting catches that; the answer is a re-validated retry, capped by
  `auto_retry_max_drift = $0.08`.
- **The model probability is NOT calibrated.** Two of the earliest live losses
  were at model confidence 99.2% and 100.0%. The market price is the better
  probability estimate. Never present `raw_probability` as a probability.

---

## 7. What is still unproven

- **`bid_imbalance` is hard-coded to 0.0** in every backtest. Nothing about it
  has been validated; conclude nothing from it.
- **The Kalshi `orderbook_fp` book-to-quote mapping is unresolved.** Offsets
  range 1-10c and flip sign between runs. Depth-derived *trading* features are
  blocked until it is settled from recorded data. (Depth is archived and shown
  as commentary, which is safe; it must not gate a trade.)
- **The live sample proves nothing yet.** About **6,000 settled signals** are
  needed before a 1c/contract edge separates from luck. As of 2026-09-21 the
  live account had **7 real trades**. Everything above rests on history.
- **Grid search cannot find this edge.** The best of 11,365 searched rules
  scored below the *median* best rule on shuffled data (p=0.99). Only
  pre-specified rules recover it. Do not trust a rule that a search found.
