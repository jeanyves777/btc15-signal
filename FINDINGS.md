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

---

## 8. Session and volatility (2026-09-21) — tested, and NOT adopted

Prompted by the live observation that the New York session looked unusually
volatile. Measured on the full BTC history, one entry per market, deployed rule
(0.85-0.93, distance >= 1.5x vol, 6-11 minutes left, 2-minute band settle).
1,453 entries. Volatility is the Binance-kline 5-minute realised figure;
quartile cuts 1.5 / 2.6 / 4.4 bps.

| slice | n | NET edge | 95% CI |
|---|---|---|---|
| all | 1453 | +0.0248 | [+0.0114, +0.0380] |
| calm BTC (bottom quartile) | 363 | +0.0228 | [-0.0045, +0.0488] |
| normal BTC (middle half) | 726 | +0.0328 | [+0.0146, +0.0504] |
| VOLATILE BTC (top quartile) | 364 | +0.0110 | [-0.0187, +0.0385] |
| US session only | 525 | +0.0281 | [+0.0054, +0.0480] |
| US session AND volatile | 181 | -0.0111 | [-0.0595, +0.0316] |

**No gate was added.** Three reasons:

1. **The gap is not a finding.** Reading the table as "volatile is worse" is the
   overlapping-CI error. Bootstrapping the *difference* directly, clustered by
   market: volatile minus rest = **-0.0185, 95% CI [-0.0526, +0.0130], p=0.262**.
   That is noise. The two lines that exclude zero are the ones with the most
   samples, which is what sample size does, not what volatility does.
2. **The rule is already volatility-normalised.** `min_normalized_distance`
   gates on `distance_bps / volatility_5m_bps >= 1.5`. When volatility doubles,
   the strike must be twice as far away in dollars to qualify. A volatility
   ceiling would be a second, cruder copy of a control that already exists.
3. **The New York session is not special.** US-only is +0.0281 against
   all-sessions +0.0248 — the same edge with fewer samples. Volatility during
   the New York session is a true observation about the *price*; it is not a
   true observation about the *edge*.

Note on units: an earlier cut of this used `contract_candles.price_close`, which
is the **contract** price, not BTC. It produced "volatility" of 1000-2700 bps per
minute, which should have been the tell. If a BTC per-minute figure is not in
single-digit-to-low-tens bps, it is not BTC.

`volatility_5m_bps`, `session` and `vol_regime` are archived on every
observation, so the live record accumulates the data to re-test this later
without a re-fetch. Re-test as a single pre-registered comparison; do not
re-slice.

---

## 9. The hourly ladder (2026-09-21) — structure measured, edge NOT measured

A second instrument was added in **shadow mode**: `KXBTCD`, the hourly "or
above" ladder. Nothing below is a result about profitability. There is no
hourly entry rule and no hourly trade.

**Structure, measured from the live API:**

- `KXBTCD` is hourly, not daily. `KXBTCD-26SEP2112` opens 15:00 UTC, closes
  16:00 UTC. The `D` in the ticker is misleading.
- One event = ~188 threshold contracts, $100 apart, one shared BRTI print.
  ~24 are quotable at any instant; the other ~164 sit pinned at 0.00/0.01 or
  0.99/1.00.
- `KXBTC` is the same expiry as ~186 mutually exclusive brackets. A different
  instrument.
- **Liquidity is much deeper than the 15-minute market.** The rung nearest spot
  carried 93,083 contracts of volume against a few hundred on a typical
  15-minute contract. Whatever else is true, fill quality should be better.
- One `expiration_value` decides all 188 rungs, so every outcome in an event is
  derivable from one number. No per-contract result fetch is ever needed.

**`updated_time` is not a quote clock.** All 188 rungs carry one of three
timestamps stamped at chain open. Measured over 20 seconds: **13 rungs changed
their quotes, zero timestamps changed.** Any freshness check built on that
field marks 100% of live snapshots stale. Staleness has to be measured across
polls - time since any quotable rung's quote last moved.

**The selection trap, stated before it can be fallen into.** A ladder offers
~24 correlated estimates at once. Taking the best net edge across them is a
maximum over correlated noise: it selects the rung where the model is most
wrong, not the one where the edge is most real. This is FINDINGS.md 7 wearing a
different hat - the best of 11,365 grid-searched rules scored below the
*median* best rule on shuffled data (p=0.99). The strike-selection rule must be
pre-registered and measured in advance, never an argmax over the live chain.
The correct question is not "which rung looks best now" but "does the rung at a
pre-specified distance from spot have an edge, measured over many hours".

**The 15-minute band must not be transplanted.** 0.85-0.93 was measured on
1,453 fifteen-minute windows. The hourly instrument has a different horizon, a
different fee burden at the same price, and a strike choice the 15-minute
market does not have. Reusing the band would be assuming the answer. Hourly
gets its own measurement, its own calibration, and its own session results, or
it does not trade.

---

## 10. The hourly ladder, measured (2026-09-21) — NO demonstrated edge

21 days: **490 settled hourly events, 90,008 rungs, 661,151 minute candles**
(`scripts/fetch_hourly.py`, `scripts/measure_hourly.py`).

Rules pre-registered before looking. The rung is chosen by a quantity
observable at entry - target PRICE or distance from spot - never by estimated
edge, because an argmax over ~24 correlated rungs manufactures an edge from
noise. One entry per event. Net of fees. Clustered by day.

**Control — the rung nearest spot** (a coin flip near 0.50, where the Kalshi
fee peaks). Pooled over 470 entries: **-0.0284 [-0.0844, +0.0251]**, 57.7% win.
Negative, as it must be. The control behaving correctly is what licenses
reading the treatment.

**Treatment — the rung whose dearer side is priced nearest 0.89**, entered at
five fixed times. Five pre-registered points, Benjamini-Hochberg corrected:

| entry | n | win | NET edge | 95% CI | raw p | BH p |
|---|---|---|---|---|---|---|
| 50 min left | 470 | 89.8% | -0.0012 | [-0.0298, +0.0265] | 0.527 | 0.527 |
| 40 min left | 470 | 90.2% | +0.0002 | [-0.0280, +0.0281] | 0.483 | 0.527 |
| 30 min left | 470 | 90.6% | +0.0041 | [-0.0238, +0.0287] | 0.375 | 0.527 |
| 20 min left | 470 | 92.6% | +0.0212 | [-0.0081, +0.0485] | 0.076 | 0.380 |
| 10 min left | 468 | 92.5% | +0.0116 | [-0.0147, +0.0353] | 0.178 | 0.446 |

**Nothing is demonstrated. The hourly ladder must not be traded.**

What the table does NOT say: it does not say hourly has no edge. The estimate at
20 minutes left (+0.0212) is close to the deployed 15-minute figure (+0.0197),
and the edge rises as entry moves later, which is the same shape the 15-minute
rule has. It is simply not resolvable here. With a CI half-width near 0.028,
this sample can only detect an edge above roughly **2.8c**, and the effect being
looked for is about **2c** - underpowered by about a factor of two. Roughly
2,000 events (~85 days) would be needed. A 90-day fetch is running.

**A mistake worth recording.** The first version of this measurement scanned
from 50 minutes left and took the first qualifying minute, exactly as the
15-minute rule does. Because a rung near 0.89 almost always exists, it fired at
50 minutes every single time - `minutes remaining: min 50, median 50, max 50`.
It was reported as "entry 10-50 minutes" when it measured one instant, 17% into
the window, against the 15-minute rule's 40-73%. "First qualifying minute" is
only a meaningful rule when qualifying is actually rare. Check the realised
distribution of any gate before believing the label on it.

Also note: in the descriptive breakdown by the ask that was actually paid, the
0.85 bucket showed +0.0473 on n=86. **That is not a finding.** It is where a
rule chosen for other reasons happened to land, which is the selection trap in
section 9 arriving by the back door.

---

## 11. Re-verification after the hourly flaw (2026-09-21)

Section 10 records a measurement that claimed "entry 10-50 minutes" while
firing at 50 minutes every time. The obvious question is whether the deployed
15-minute rule was measured the same way. **It was not.** Realised entry times,
deployed rule, 1,453 entries:

| minutes left | 10 | 9 | 8 | 7 | 6 |
|---|---|---|---|---|---|
| share of entries | 19.8% | 17.2% | 20.5% | 21.1% | 21.5% |

Near-uniform. The failure is specific to a strike LADDER: with 188 rungs a
contract near any target price always exists, so "first qualifying minute"
collapses to "first minute scanned". With the single strike of a 15-minute
market, qualifying is genuinely rare and the distribution spreads out. Two
internal checks also pass - without the settle rule entries appear at 11
minutes left, and with it 11 correctly vanishes, since two consecutive
qualifying minutes make 10 the earliest possible entry.

**Sample size corrected.** The deployed rule yields **1,453** entries, not the
1,674 quoted earlier in places. The larger figure came from a run whose scan
loop used `break` where it should have continued, and is superseded everywhere.

**The band-settle rule was re-derived**, because `entry_band_settle_s = 120` is
live config whose justification came from that same superseded run. Paired on
the 1,453 markets that qualify under both rules, the delay itself is harmful:
entering at the first qualifying minute returns +0.0423 [+0.0286, +0.0556]
against +0.0248 [+0.0114, +0.0378] after waiting, a difference of **-0.0175
[-0.0189, -0.0161]**.

That is not a reason to remove the rule, and reading it as one would be a
lookahead error. The settle rule both DELAYS entry and EXCLUDES the markets that
never hold the band, and those excluded markets are bad: **-0.0164
[-0.0333, +0.0004]** on 1,513 entries. You cannot keep the good markets and
enter them early, because at minute one you do not know which ones will hold.
Comparing only the two implementable rules:

| rule | n | NET edge | 95% CI | total |
|---|---|---|---|---|
| enter at 1st qualifying minute | 2966 | +0.0124 | [+0.0011, +0.0228] | +$36.70 |
| **wait 2 minutes in band (deployed)** | 1453 | **+0.0248** | [+0.0114, +0.0378] | +$36.09 |

Twice the edge per contract for half the trades and half the capital at risk,
at the same total. **Keep `entry_band_settle_s = 120`.** The earlier chat
figures (+0.0219 vs +0.0080) are superseded by +0.0248 vs +0.0124; the
direction was right, the magnitudes were not.

**The open opportunity.** The delay costs 1.75c and the filter is worth more
than that. Anything observable AT MINUTE ONE that predicts whether the band
will hold would let entry happen early on the markets worth keeping. The
archive records the full per-minute path, so this is answerable. It may also
bear on the live fill problem: on 2026-09-21 two of three auto orders did not
fill, and in both cases the order was placed well above the price at which the
signal first appeared (signal 65% -> order 86%; signal 81% -> order 91%). The
mechanism is NOT established - that is an inference from three orders, not a
measurement. What IS certain is that the backtest above credits a fill at the
delayed price, and live trading got one fill in three. Whatever the cause, a
measured edge that assumes execution overstates what is collectable.

---

## 12. The clock, not the band (2026-09-21)

A manual entry at 0.72 on `KXBTC15M-26SEP211145-45` settled at $1.00 while the
bot placed no order. The archive says why, and it is not the price band.

| remaining | ask | failed gates |
|---|---|---|
| 316s | 0.84 | contract price band |
| 291s | 0.84 | contract price band |
| **279s** | **0.921** | **None** |
| **267s** | **0.921** | **None** |
| 255s | 0.83 | contract price band |

The setup qualified **completely**. `entry_to_seconds = 330` stopped
`primary_signal` before it looked: at 279s the function returns at its first
line, which is why the service log holds exactly one entry for the whole
window. Had it looked, `entry_band_settle_s = 120` would have blocked it
anyway - the 0.921 streak lasted 24 seconds.

**Two gates are invisible to `failed_gates`.** That column records the ENTRY
RULE only. The entry-time window and the band-settle are applied afterwards, in
the trading path. A row reading "gates: None" therefore does NOT mean an order
was placed, and any study that counts clean rows as trades will overcount. Of
256 fully-clean polls in the live archive, **97 fall outside the entry window**,
and 3 markets qualified ONLY outside it.

**A live-versus-measured discrepancy.** Every backtest here uses 6-11 minutes
remaining (360-660s). The deployed config is `entry_from_seconds = 630`,
`entry_to_seconds = 330`. The measured rule and the running rule are shifted 30
seconds apart at both ends. Not yet quantified.

**The manual trade is archived** in `manual_trades` (`scripts/record_manual_trade.py`)
so research can separate signal price from fill price from what was actually
reachable: signal 0.67, filled 0.72 (acting late cost 5c), settled 1.00, net
**+0.2658**; cashing out at the available 0.94 would have returned +0.2058.
Holding the last two minutes earned **+0.06 more while the whole position stayed
exposed**. One observation. The measured verdict on cash-out remains -0.0006
(noise) from 5,753 paired trades; this does not move it.

---

## 13. The side-convention trap (2026-09-21)

**Every band table in this file above section 12, and `scripts/compare_series.py`,
pick the side as the DEARER contract:**

```python
up, dn = yes_ask, 1 - yes_bid
ask = max(up, dn)
```

**The live service does not.** `model.py:18` sets
`side = "UP" if signed_distance >= 0 else "DOWN"` - by where BTC sits relative
to the strike - and `main.py` then reads *that* side's ask. The two agree where
the contract is dear and diverge exactly at the cheap end:

| favourite ask | side disagreement | mean gap |
|---|---|---|
| 0.95-0.99 | 2.3% | 0.021 |
| 0.85-0.89 | 13.4% | 0.097 |
| 0.70-0.74 | 25.0% | 0.108 |
| 0.60-0.64 | 33.0% | 0.076 |

So the published tables fairly describe the deployed rule at 0.85+, and **do not
describe it below**. On the live convention the deployed band measures
**+0.0220 [+0.0081, +0.0360]** against the published +0.0248 - same verdict,
slightly smaller. Any future study of the cheap end that uses the published
convention is measuring a rule the bot does not run.

## 14. Below the band: measured, and the answer is no (2026-09-21)

Full per-bucket study on 6,435 settled markets / 68 days, live side convention,
BH-corrected over nine 0.05 buckets.

| band run AS the band | n | NET | 95% CI | BH p |
|---|---|---|---|---|
| 0.70-0.74 | 336 | +0.0325 | [-0.0132, +0.0758] | 0.185 |
| 0.75-0.79 | 409 | +0.0036 | [-0.0374, +0.0430] | 0.485 |
| **0.80-0.84** | 428 | **-0.0012** | [-0.0368, +0.0332] | 0.527 |
| 0.85-0.89 | 511 | +0.0289 | [+0.0034, +0.0527] | 0.070 |
| 0.90-0.94 | 647 | +0.0170 | [-0.0013, +0.0342] | 0.097 |

The bucket immediately below the floor is the flattest line in the table. The
two pre-specified hypotheses, each tested once, with the difference bootstrapped
directly:

- `min_ask 0.80` - deployed = **-0.0055 [-0.0166, +0.0047]**, p=0.30
- `min_ask 0.70` - deployed = **-0.0100 [-0.0240, +0.0035]**, p=0.15

Both negative, neither significant. The trades a lower bound would ADD measure
+0.0076 and +0.0091 with intervals through zero. Total dollars rise on the point
estimate (2.5x the trades at half the edge) but the wider rule's interval
includes zero, and 50 entries/day collides with `auto_max_trades_per_hour = 6`
anyway. **Do not lower the bound.**

**The trend confound is settled**: the operator's exact regime - momentum
aligned, 4h trend >= 50bp - measures **-0.0019 [-0.0457, +0.0404]** on the
pooled 0.70-0.84. There is no trend artifact masquerading as an edge because
there is no edge to attribute.

**One positive sub-band result, deliberately NOT adopted.** 0.65-0.69 as its own
band: +0.0709 [+0.0209, +0.1203], n=284, BH p=0.038 in the 9-bucket family but
**0.079 across all 35 buckets tested**. It survives bucket-edge jitter, all four
quarters of history, and a negative control. It dies on two things: it collapses
without the settle rule (settle=1 gives **-0.0061**), and an open 0.55-0.99 band
- what a widened rule would actually trade - captures the same region at only
+0.0126 [-0.0251, +0.0487] and measures +0.0024 overall. **It is a grid-found
rule**, and section 7 says what those are worth. Recorded as a pre-registered
hypothesis to measure FORWARD, never as a band change.

**What the data cannot resolve**: the difference test resolves +-1.5c. A genuine
1c/contract gain from lowering the bound is invisible here; the history would
need to roughly triple. 0.80-0.84 alone has an MDE of 5c.

**Also found, not acted on**: 0.85-0.93 does BETTER when the 4h trend runs
AGAINST the bet (+0.0416 [+0.0248, +0.0577]) than with it (+0.0008
[-0.0222, +0.0228]), difference p=0.0045 - the opposite sign to intuition. A
single slice of an already-sliced sample. Hypothesis, not a gate.

---

## 15. The hourly ladder, settled (2026-09-21) — no edge, and the promising cell was noise

Section 10 measured 490 events over 21 days, found nothing significant, and
said the sample was underpowered by about a factor of two. The history was
extended to everything Kalshi holds for the series: **1,584 settled hourly
events over 72.8 days, 46,250 rungs, 2,148,976 minute candles.** Same five
pre-registered entry times, same rung-selection rule, same BH family. Nothing
was re-chosen.

| entry | n | win | NET edge | 95% CI | BH p |
|---|---|---|---|---|---|
| 50 min left | 1508 | 91.0% | +0.0077 | [-0.0069, +0.0215] | 0.705 |
| 40 min left | 1508 | 89.3% | -0.0116 | [-0.0294, +0.0053] | 0.916 |
| 30 min left | 1507 | 89.7% | -0.0100 | [-0.0241, +0.0038] | 0.916 |
| 20 min left | 1501 | 91.5% | **+0.0047** | [-0.0129, +0.0224] | 0.716 |
| 10 min left | 1497 | 91.6% | +0.0011 | [-0.0120, +0.0141] | 0.716 |

Control (rung nearest spot, 20 min left): -0.0110 [-0.0305, +0.0098].

**The 20-minute cell went from +0.0212 to +0.0047.** On 21 days it was the best
line in the table, raw p=0.076, and section 10 noted it sat close to the
deployed 15-minute figure of +0.0197 and rose as entry moved later. Tripling the
data collapsed it to a fifth of its size. That is what the most promising cell
in an underpowered table does, and it is the cleanest example in this file of
why a raw p near 0.08 on a small sample is not a finding waiting for
confirmation.

**This is no longer an underpowered null.** CI half-widths are now 0.7c to 1.8c
against 2.8c before. An edge the size of the deployed 15-minute rule (+0.0197)
would have shown at four of the five entry times. It did not.

**Verdict: the hourly ladder has no tradeable edge and the question is closed**
unless something other than price-targeted rung selection is proposed. Shadow
recording continues, because the live archive captures depth, spreads and the
full chain path that this candle study cannot see - but no hourly rule should
be written against this instrument on the strength of historical quotes.

The 15-minute strategy is unaffected: different instrument, different horizon,
measured separately, still +0.0220 [+0.0081, +0.0360] on the live side
convention.

---

## 16. "They keep winning and we are just watching" — tested (2026-09-21)

The operator repeatedly saw declined setups settle as winners: a 72c manual
entry that paid $1.00, and a DOWN contract at 94.1% with 4:53 left that the bot
ignored. Both fell outside the entry window. The window had never been tested,
so it was tested. LIVE side convention (see section 13), 68 days, everything
else deployed.

| entry window (minutes left) | n | NET | 95% CI | total |
|---|---|---|---|---|
| **6-10 (DEPLOYED)** | 1243 | **+0.0218** | [+0.0078, +0.0357] | +$27.05 |
| 5-11 | 1594 | +0.0173 | [+0.0038, +0.0303] | +$27.61 |
| 4-12 | 1860 | +0.0180 | [+0.0053, +0.0298] | +$33.41 |
| 3-13 | 1991 | +0.0149 | [+0.0027, +0.0267] | +$29.76 |
| 2-14 | 2058 | +0.0127 | [+0.0004, +0.0244] | +$26.14 |
| 0-15 (whole window) | 2072 | +0.0122 | [+0.0007, +0.0235] | +$25.32 |
| 6-10, ceiling raised to 0.97 | 1974 | +0.0172 | [+0.0065, +0.0276] | +$33.90 |
| 1-14, no settle rule | 3947 | **-0.0006** | [-0.0102, +0.0094] | -$2.23 |

**The deployed window is the best per contract of everything tested**, and the
degradation is monotone as it widens. Widening buys volume at the cost of
quality. Paired difference tests against the deployed rule, bootstrapped
directly over markets:

| alternative | per contract | total dollars |
|---|---|---|
| window 4-12 | -0.0038 [-0.0129, +0.0050] | +$6.36 [-$8.26, +$20.16] |
| ceiling 0.97 | -0.0046 [-0.0122, +0.0030] | +$6.85 [-$4.05, +$16.91] |
| **both together** | **-0.0113 [-0.0222, -0.0007]** | +$2.62 [-$17.03, +$21.20] |

Loosening either gate alone is a wash - no significant gain or loss either way.
Loosening **both** is significantly WORSE per contract and gains nothing in
total. Removing the settle rule while widening the window destroys the edge
outright (-0.0006).

**Answer: the bot is not leaving money on the table.** Declined setups do win
often, because they are priced to win - a contract at 94% wins about 94% of the
time, and the break-even at 94c after fee is about 94.4%. Trading them does not
measurably improve the result.

**The honest residual.** Every total-dollar point estimate is positive (+$6.36,
+$6.85, +$2.62) with intervals through zero, so slightly more total profit may
exist and this data cannot resolve it. At $1/contract that is about **9 cents a
day** over 68 days, against `auto_max_trades_per_hour = 6` and a
one-position-at-a-time guard that would block much of the extra volume anyway.
Not worth trading quality for.

**Caveat on this study**: seven window choices and three band variants were
scanned before the difference tests were run. The differences themselves were
tested directly and paired, which is the right test, but "4-12" was chosen as a
comparator after seeing the table. Treat the magnitudes as indicative; the
verdict - no significant improvement available - does not depend on which
comparator was picked, since the deployed rule is top of the table on the
measure that matters.

---

## 17. The deployed window did not match the measured one (2026-09-21) — FIXED

Every study in this file uses `6 <= remaining <= 11` (`scripts/compare_series.py`).
Backtest snapshots sit on exact minute boundaries - `remaining = 15 - elapsed`
in `features.py` - so the measured range is **360-660 seconds**.

The live config was `entry_from_seconds = 630`, `entry_to_seconds = 330`. Half a
minute adrift at **both** ends. The bot was therefore:

- trading a **330-359s** band that no study has ever covered, and
- ignoring **630-660s**, which every study includes.

Changed to 660 / 360. Measured 6-11 under the live side convention (section 13):
**+0.0220 [+0.0077, +0.0360] on 1,359 entries, +$29.96** over 68 days - which
independently reproduces the subagent's figure for the same window to four
decimal places. Against the 6-10 window used in some of this session's tests the
difference is +0.0003 [-0.0044, +0.0043], not distinguishable, but 6-11 captures
116 more entries.

`tests/test_service_window.py` now asserts the config equals 6 and 11 minutes so
this cannot drift again silently.

**This is not a loosening, and it does not chase the recent misses.** It tightens
the late end. `KXBTC15M-26SEP211215-15` was eligible at 348s on 2026-09-21,
inside the old window and OUTSIDE the new one - though the 120-second settle
blocked it anyway, so no trade either way. The change was made because the
deployed rule should be the rule that was measured, not because it captures any
particular observation.

**What was rejected at the same time.** Sections 14 and 16 measured every
loosening the live record suggested. All have negative point estimates per
contract; none is significantly better on total dollars; window-plus-ceiling
together is significantly worse (-0.0113). The settle curve is monotone -
+0.0113 / +0.0218 / +0.0297 / +0.0364 at 1/2/3/4 minutes - so waiting longer
produces better trades, and shortening the settle to catch fast movers runs
against the gradient. No gate was loosened.

---

## 18. Live evidence is the real evidence (2026-09-21)

The operator's objection: results and live-collected data are the evidence,
not backtests on past data. That is substantially correct, and it is now a
report - `scripts/live_evidence.py` - which uses only what actually happened
and touches no historical candle.

It separates three things that one P&L figure hides:

| | what it measures | can a backtest see it? |
|---|---|---|
| RULE | did the gates pick winners, traded or not | yes |
| EXECUTION | what getting the trade ON cost | **no** |
| REALISED | the account: rule + execution + variance | no |

**The execution section is the part no historical study can produce**, and it
is the operator's point made concrete: **21 orders submitted, 9 filled (43%),
12 missed.** A backtest credits all 21.

Live, as of 17.2 hours of settled signals:

```
RULE       all signals (paper)     n=61  +0.0961/ct  [+0.0120, +0.1725]
           inside 0.85-0.93 band   n=14  +0.1198/ct  NO INTERVAL - 0 losses
EXECUTION  fill rate 43%; fills land -0.0151 vs the decision price
REALISED   n=9  -0.1034/trade  total -$0.9306
GAP        realised - paper on the same trades = +0.0369/ct
```

**Two numbers this report produced first time round were wrong, and the
mistakes are worth keeping.**

1. It printed a bootstrap CI of [+0.1076, +0.1308] for the banded signals. That
   sample is **14 wins and 0 losses**. A bootstrap can only resample what it was
   given, so it never draws a loss; the interval describes price variation among
   winners, not uncertainty about the mean. It now REFUSES an interval below 3
   losses and prints "NO INTERVAL" instead.
2. It claimed the banded sample needed "~5 more samples" to resolve a 2.2c edge,
   because it estimated the standard deviation from that same all-wins run.
   Sample size now uses the THEORETICAL payoff variance q(1-q) with q taken from
   the price, which gives **~1,586 trades, about 79 days**.

The correct reading of the live record: 14 of 14 banded signals won, the 95%
lower bound on that win rate is **78.5%**, and break-even at 0.89 after fee is
**89.7%**. The lower bound does not clear break-even. The live data is
**consistent with the measured edge and does not establish it** - and cannot,
for roughly another 80 trading days.

**So both objections are right at once.** Backtests cannot see execution and
overstate what is collectable; live data is the only thing that can settle it
and is nowhere near able to yet. The response is not to pick one, but to record
execution from now on (section 12, the `executions` table) so that when the live
sample matures it can answer the question the candles never could.

---

## 19. Backtesting on the live tape instead of candles (2026-09-21)

Section 18 answered "live results beat backtests" by reporting the live record.
This goes one step further and does the **backtest itself on live-collected
data**: `scripts/replay_live.py` replays the `observations` table - the quotes
the running service actually saw, at the cadence it actually polled, with the
settlement it actually observed - through the deployed gate stack. No candle is
read and no price is reconstructed.

Three things the tape has that a minute candle cannot:

1. **The real ask on our side** at the instant of decision, not a mid or close.
2. **The real cadence.** The service sees the book about every 12s and can only
   act at those instants. A minute-candle study is simultaneously too coarse to
   see a 24-second band touch and too generous about when it may act.
3. **The 120s settle gate.** `entry_band_settle_s` is a rule about the *path*
   the price took, not a price level. It needs sub-minute quotes and **cannot be
   evaluated on minute candles at all** - and it is the gate currently deciding
   whether this system trades.

**Two corrections the build forced, both worth keeping.**

- The stored `rule_match` column is **not trustworthy across the tape**.
  `strategy.json` was edited mid-record (0.80-0.99 -> 0.85-0.93 at 14:00 UTC),
  so earlier rows carry a verdict from a rule no longer deployed. Every gate is
  recomputed from raw features against the rule on disk.
- The first run reported the 12:15 window as having "held the band 121s" and
  been skipped anyway, which looked like a gate bug. It was a reporting bug:
  that streak was reached with **120s left on the clock**, two minutes past the
  entry window and failing three other gates. Taking the maximum streak over
  the whole window describes a moment the gate could never have acted on. It now
  reports the longest streak reached at a poll that passed every other gate.

**The funnel, on 22 windows / 1,463 live quotes / 5.2 hours:**

```
windows on the tape                      22
...whose ask entered the band            19
...eligible on ALL gates at some poll    13
...and held the band   0s -> TRADE       13
...and held the band  60s -> TRADE        5
...and held the band 120s -> TRADE        3
```

**The settle gate is the binding constraint, by a wide margin.** Thirteen
windows qualified on price, distance, model and momentum; three survived the
wait. The other ten topped out at 24-85 seconds in band before the ask moved or
the entry window ran out. This is the mechanical explanation for the live
service placing **no order between 14:52 and 16:46 UTC** while logging
"eligible" repeatedly.

**Why none of that says the gate is wrong.** The tape contains almost no
downside inside the band:

```
quotes in band, WINNING windows          203
quotes in band, LOSING  windows            3
```

Both losing windows (11:45, 15:45) were excluded by the band almost
mechanically - one put three quotes in the band, the other none. So every win
rate in the report is near 100% **by construction**, and the headline that the
120s wait "declined +1.2166 of P&L at the touch ask" is not a cost measurement:
on a tape whose band holds no losers, *any* filter can only look expensive.
This is the same shape as the section 18 bootstrap failure - the downside is
absent from the sample - and it is why the report prints section 2b above
section 3 rather than below it.

**Execution, carried over and applied rather than assumed:** 21 orders
submitted, 9 filled (43%), fills landing -0.0151 against the decision price (7
better, 2 worse). Applied to the replay, the haircut goes on the **count, not
the price** - a missed order is a trade that never happened, not a loss. The
three deployed-gate trades are +0.3104 assuming fills and +0.1330 at the live
fill rate.

**What this settles and what it does not.** It settles the *mechanics*: which
gate stops which window, and what the ask did while the clock ran. It settles
nothing about edge - 3 trades against the ~1,600 of section 7 is a rounding
error, and the 95% lower bound on the deployed gate's win rate is 43.8% against
a break-even of 89.7%.

**The change NOT made.** The obvious move from this report is to loosen
`entry_band_settle_s`, since it is refusing ten of thirteen qualifying windows
that all won. That is exactly the inference the tape cannot support, and the
120s value came from a measurement that did clear zero where entering on the
first qualifying minute did not (`store.band_streak_seconds`). The gate stays
until a tape with in-band losses in it can price the trade-off.

**Open, unverified:** the `executions` table is still empty. It is covered by
`tests/test_execution_log.py`, but the instrumentation landed at ~15:52 UTC and
the last live order was 14:52 UTC, so **it has never yet written a production
row**. The first order placed after a settle-gate clearance is what proves it.

---

## 20. The forward test section 14 asked for, and the day-effect trap (2026-09-21)

The live feed for 2026-09-21 shows a long run of declined sub-band setups
settling as winners: signals at 63-81% refused on the price band, then "✅
right", over and over, while paper climbed to +6.16 and live sat at -0.93. The
natural reading is that the band is too tight.

Sections 14 and 16 already tested that reading on 68 days and said no. What
neither had was the thing section 14 explicitly asked for: 0.65-0.69 was
recorded "as a pre-registered hypothesis to measure FORWARD, never as a band
change", and **nothing measured it forward.** `scripts/forward_test.py` does.

The historical study ends at the last kline, 2026-09-20 23:20; the live signal
record begins 2026-09-20 23:09. The eleven-minute overlap is immaterial, so the
live record is genuinely out of sample.

**The statistic is excess over the market's own price**, not win rate:

```
excess = wins - sum(contract_price)
```

A contract at 0.70 that wins 70% of the time has zero excess and, after fee, is
a losing trade. Win rate flatters any band full of high-probability bets; the
price already charged for those. This is why the feed's "89% win rate" and the
band-qualified "24/27 (89%)" being identical is the interesting number, not the
89% itself.

**The control, which has to be read first.**

```
settled live signals    63
wins                    56
wins the PRICES implied 48.9
excess                  +7.1  (+0.112/signal)
z against fair pricing  +2.21
```

**The whole tape beat its prices, not one band.** On a day like this every band
beats its own price at once and whichever band is under suspicion looks
vindicated. A band is only interesting if it beat its prices by MORE than the
rest of the tape did.

**Measured that way, no band separates from the day:**

| hypothesis | forward | z | vs rest of tape |
|---|---|---|---|
| 0.65-0.69 (registered) | 9/11 @ 0.67 | +1.05 | +0.043 — no interval, 2 losses |
| **0.70-0.84** (lower the floor) | 26/30 @ 0.79 | +1.07 | **-0.064 [-0.222, +0.088]** |
| 0.85-0.93 (deployed) | 14/14 @ 0.87 | +1.43 | +0.020 — no interval, 0 losses |

Every individual z is below 1.96 while the tape's is above it. **The band the
feed is full of - 0.70-0.84 - measured WORSE than the rest of the tape on the
point estimate**, which is the opposite of the "money left on the table"
reading, though its interval spans zero and settles nothing either way.

**So the answer to the feed is: today was a good day at every price.** The
declined signals won because almost everything won - including, at the same
time, the 14/14 inside the deployed band. Attributing that to the band boundary
requires the boundary to have done something, and on this tape it did not.

**Nothing adopted, nothing changed.** Each hypothesis is 90x to 326x short of
the sample it needs to resolve a 2.2c effect. The value of this section is the
method: the control line makes a day-wide mispricing visible as a day-wide
mispricing, which is the specific way a short live record misleads. It is the
same failure as the section 18 bootstrap and the section 19 all-wins band, in a
third costume.

Re-run `python scripts/forward_test.py` daily. It becomes evidence only with
time, and the registered hypotheses are now written down where a later result
cannot be quietly reinterpreted.

---

## 21. The band floor lowered to 0.70 (2026-09-21) — operator decision, logged as a live experiment

**Changed:** `strategy.json` `min_ask` 0.85 -> 0.70. `max_ask` stays 0.93,
`entry_band_settle_s` stays 120. Previous file kept at `strategy.json.bak.band85`.

**This is an operator decision taken against the measured evidence**, made with
that evidence in front of it, and it is recorded here as such rather than
rewritten into a justification. Sections 14, 16 and 20 all measured this region
and none of them found a reason to widen:

- s14, 68 days: `min_ask 0.70` - deployed = **-0.0100 [-0.0240, +0.0035]**,
  p=0.15. Negative point estimate, not significant. The bucket immediately
  below the floor (0.80-0.84) is the flattest line in the whole table.
- s16: declined setups win because they are **priced** to win; trading them did
  not measurably improve the result.
- s20, forward: on the live tape the 0.70-0.84 region measured **-0.064/signal
  against the rest of the same tape** [-0.222, +0.088]. Opposite sign to the
  intuition, interval through zero, settles nothing.

**What the change was chosen on.** Trade count, from the live-tape sweep:

| floor | trades today | won | avg ask | net/ct | in-band quotes from LOSING windows |
|---|---|---|---|---|---|
| 0.85 | 3 | 3 | 0.890 | +0.1035 | 3 |
| 0.80 | 7 | 7 | 0.861 | +0.1305 | 9 |
| **0.70** | **13** | **13** | **0.835** | **+0.1550** | **23** |
| 0.65 | 14 | 14 | 0.806 | +0.1834 | 52 |

Lowering the floor helps twice: it admits cheaper contracts AND makes the 120s
settle gate far easier to clear, because a wider band is one the price stays
inside for longer. That is why the count moves 3 -> 13 rather than marginally.

**The 14/14 and 13/13 in that table are not evidence.** 2026-09-21 beat its own
prices across every price level at once (s20, z=+2.21). The loser-exposure
column is the honest read of the risk: a 0.70 floor puts the band across ~8x
more of the losing windows' price action than 0.85 did, and today's clean sweep
dodged all of it on a sample of two losing windows. **Expect losses this band
would have taken.**

**Guardrails that now actually bind** (`autotrade.auto_block_reason`, all hard
stops read from durable state before every order): daily loss floor $10,
one open position at a time, 6 trades/hour, per-day cap, minimum spacing. At
~13 trades/day at ~$0.84 the daily loss floor is the one that matters - it is
roughly one bad day away, by design.

**Applied without a restart.** `EntryRule.load` runs every poll, so the file is
hot-reloaded; it was written atomically (temp file + `os.replace`) because
`load` has no guard around `json.loads` and a half-written file read mid-poll
raises inside the loop. Confirmed live at 17:11:27 UTC: a 0.81 ask evaluated
`rule_match=1, failed_gates=None`, where a 0.84 ask three minutes earlier had
failed `contract price band`.

**What decides whether this stays.** Re-run `scripts/forward_test.py` daily. The
question is not "did it win" - on a trending day everything wins - but whether
0.70-0.84 beats **the rest of the same tape**, which is the only comparison
that separates the band from the day. s14 says ~2,700 signals to resolve 2.2c.
**Revert with `cp strategy.json.bak.band85 strategy.json`** - it takes effect on
the next poll, no restart.

---

## 22. Why the auto orders missed, and what support/resistance is worth (2026-09-21)

Lowering the floor to 0.70 (section 21) worked immediately: the 13:30 window
qualified within four minutes. Then **all three orders missed**, and the
operator took the trade by hand at 0.81. It settled DOWN, a winner:
**+$0.1792 net**, recorded in `manual_trades`.

**The `executions` table wrote its first production rows** - section 19 listed
it as never having done so - and they diagnose the miss immediately:

```
17:19:29  att=1  decision_ask=0.85  filled=0  decision_to_submit=2176ms  round_trip=178ms
17:21:20  att=2  decision_ask=0.85  filled=0  decision_to_submit=2018ms  round_trip=225ms
17:21:59  att=3  decision_ask=0.81  filled=0  decision_to_submit=2026ms  round_trip=235ms
```

**The network is not the problem.** Kalshi answers in ~200ms. The order goes out
~2,000ms after the decision it was priced from, and the orders are
`immediate_or_cancel` - they fill against resting size or die. An IOC at a
two-second-old touch with one cent of allowance cancels.

**Where the two seconds went.** `now_ms` is stamped at the TOP of the poll
cycle, and before the order the loop ran: pending settlements (`kalshi.result`
per row, plus a Telegram settlement report), **the hourly ladder's shadow poll -
188 rungs** - then `active_market`, the Binance snapshot, and the observation
write. A recorder that by design never trades was standing in front of real
money.

**Fixed:** the hourly shadow block now runs AFTER `primary_signal` /
`reversion_signal`. Its original comment explained it sat early so the archive
kept running between windows, where the loop `continue`s - so it is now called
on BOTH paths rather than merely moved, and the gap is still covered. Deployed
by restart at 13:32 local.

**Also fixed, an instrumentation bug that hid the variable under test.**
`executions.limit_submitted` recorded `claimed.entry_limit`, not the limit
actually sent, which is `entry_limit + entry_slippage` capped at 0.99. Three
orders logged as 0.85 were really submitted at 0.86 - and the slippage
allowance is precisely what decides whether a fill happens. The column that
exists to explain misses was recording the wrong number.

**NOT YET VERIFIED.** No order has been placed since the restart, so the
latency fix is deployed and **unmeasured**. The next `executions` row is the
test: `decision_to_submit_ms` should fall well below 2,000ms. If it does not,
the remaining cost is the Binance snapshot and `active_market`, which sit
between the ask being read and the order going out.

### "Keep checking a refused signal so it can still qualify"

Already true, and the archive proves it. Every poll re-evaluates every gate
from scratch - `observations.failed_gates` is written fresh about every 12
seconds, and a setup refused at 69% is re-checked 12 seconds later:

```
17:36:03  ask=0.69 -> contract price band
17:36:16  ask=0.69 -> contract price band
17:36:42  ask=0.62 -> contract price band
```

Nothing is latched or dropped. What fires only once per window is the TELEGRAM
ALERT (`strategy_alerts`, one row per strategy+window - section 3's alert lock).
The trading path is not gated by it. So a refused signal that later enters the
band **will** be taken; it simply will not produce a second "ENTRY READY"
message. The thing to change, if anything, is the notification - not the rule.

### Support and resistance: measured, and NOT established

The operator is right that it is missing: `key_level` exists only in the
reversion strategy, set to the current window's high or low. The primary rule
has no notion of a level. `scripts/measure_levels.py` tests ONE pre-specified
hypothesis over 71 days - if a level stands in the way of the move that beats
us, it has to break first, so those setups should settle better:

```
                                   n       won     net       95% CI              p
ALL qualifying setups           15369    83.7%  +0.0126  [+0.0025, +0.0223]   0.009
a level BLOCKS the adverse move  7370    84.8%  +0.0198  [+0.0049, +0.0341]   0.003
no level in the way              7999    82.6%  +0.0059  [-0.0093, +0.0202]   0.212
```

Read alone, that looks decisive: one arm's interval excludes zero and the
other's does not. **It is not the test.** Section 16 makes exactly this point,
and the difference has to be bootstrapped directly - on shared clusters, since
one market contributes minutes to both arms:

```
blocked - clear   +0.0140/contract  [-0.0067, +0.0344]  p=0.090
```

**The interval includes zero.** The point estimate is the largest single-feature
effect measured in this file and it still is not established at n=15,369. No
gate is added. `CONFIRM=30` and the 24h level lifetime were picked once, before
running, and not tuned - tuning them would turn this into the grid search
section 7 warns about.

**No lookahead:** a pivot is timestamped at CONFIRMATION, not formation. A swing
high is only known 30 minutes after the bar that made it, and using the
formation time would identify the level with the very bars that prove it held.

---

## 23. The support/resistance gate, deployed (2026-09-21) — operator decision

Section 22 measured levels and did not adopt them: the difference between
filtering and not filtering was +0.0140 [-0.0067, +0.0344], p=0.090. The
operator's decision was to apply it anyway. Logged here as taken against the
measurement, with the evidence beside it rather than rewritten to suit.

**What the evidence actually supports.** The gated arm on its own is
+0.0198 [+0.0049, +0.0341], n=7,370, an interval excluding zero. So trading
only the setups with a level in the way is defensible on its own terms. What is
NOT established is that it beats not filtering. Both readings are true at once
and the second is the one this gate is betting on.

**The cost, stated up front: it refuses about half.** 7,999 of 15,369
qualifying setups had no level in the way. On top of the 0.70-0.93 band and the
120s settle gate, expect a materially lower trade rate.

**Design.**

- `levels.py` holds ONE definition of a pivot and of "the level in the way",
  imported by both `scripts/measure_levels.py` and the live path. A level shown
  in Telegram computed differently from the level that was measured would be
  worse than showing nothing - it would read as confirmation of a result that
  was never about it. The study was re-run after the refactor and reproduces
  every figure exactly (n=15,369 / 7,370 / 7,999, same intervals).
- **No lookahead.** A pivot is stamped at CONFIRMATION, `CONFIRM=30` minutes
  after the bar that formed it. Using formation time would identify a level
  with the very bars that prove it held. `test_a_pivot_is_not_knowable_until_it_is_confirmed`
  pins this.
- **Off the order path.** Levels need ~25h of one-minute bars; the live
  snapshot fetches 16. That second Binance call is exactly the latency that
  cost three fills in section 22, so `LevelTracker` refreshes on its own
  300-second clock AFTER the trading path, and the rule only ever reads the
  cache.
- **Unknown levels REFUSE.** An empty or failed cache reports
  `levels unavailable`, distinct from an ordinary `support/resistance`
  rejection, and blocks. Following `autotrade.auto_block_reason`: a check that
  cannot be evaluated answers no. The distinct wording exists because a silent
  stop is the failure mode that has already cost this system four hours once.
- **Default OFF in code** (`require_blocking_level: bool = False`) so the
  package's own default remains the measured rule; it is ON only in the
  deployed `strategy.json`. Previous file at `strategy.json.bak.nolevels`.

**Confirmed live** at 17:55:25 UTC after restart: a 0.972 ask recorded
`failed_gates = "contract price band, support/resistance"`. That it names the
gate rather than `levels unavailable` proves the tracker had loaded.

**A guard that worked.** Between writing `strategy.json` and restarting, the
running process logged `EntryRule: ignoring unknown setting(s)
['require_blocking_level']` on every poll instead of dying - `_known_fields`
doing precisely the job it was added for after an earlier unknown key took the
service down mid-loop.

**How this gets settled.** `scripts/forward_test.py` for the band and
`scripts/measure_levels.py` for the level. The honest test is not whether gated
trades win - at these prices they mostly will - but whether they beat the
setups the gate refused. That comparison needs the refused ones to keep being
recorded, which they are: every poll writes `failed_gates`.

**Revert:** `cp strategy.json.bak.nolevels strategy.json`, effective next poll,
no restart needed.

---

## 24. The cash-out sold into a price that was not there (2026-09-21)

First auto fill since the band change: KXBTC15M-26SEP211400-00, DOWN at 0.76
against a limit of 0.82, **filled 5c better than the limit**, settled a winner
at +$0.23. Live moved -0.93 -> -0.70 over 10 trades.

**The latency fix from section 22 did NOT work, and the fill does not prove it
did.** The filled order measured `decision_to_submit_ms = 2171`, statistically
identical to the three misses (2176 / 2018 / 2026). Moving the hourly shadow
recorder off the pre-order path changed nothing measurable, so the ~2s lives
elsewhere - the Binance snapshot and `active_market` both sit between `now_ms`
and the order. This one filled because the book moved IN OUR FAVOUR (5c
better), not because it was faster. The `limit_submitted` fix does work: it
recorded 0.82 = 0.81 + slippage, where the old code would have logged 0.81.

**Then the cash-out fired and filled nothing.** Four minutes later:

```
17:56:54   yes_ask 0.021  ->  inferred bid 0.979  ->  IOC sell at 98%  ->  NO FILL
Kalshi app at the same moment:                        Cash out = $0.93
```

**Root cause, and it is not arithmetic.** `KalshiMarket` carried asks ONLY -
there were no bids in the model at all - so the exit inferred one:

```python
bid = 1 - contract.ask("DOWN" if side == "UP" else "UP")
```

On Kalshi `no_bid = 1 - yes_ask` holds exactly, so the number was right as
arithmetic and wrong as a price: it is a top-of-book quote, and the executable
price was ~5c worse. That is the 1-10c book-to-quote offset section 7 records
as unresolved, showing up for the first time somewhere that spends money.

**The order was also submitted AT that quote.** Entries have `entry_slippage`
because an immediate-or-cancel order at the touch only fills if the quote is
real and still there - three entries missed that way hours earlier. Exits had
no equivalent at all.

**Why it mattered more than the miss.** The capture gate was evaluated on the
same phantom:

```
paid 0.76, cash_out_capture 0.90  ->  needs a bid of 0.976
quoted 0.979                      ->  fired, by 0.003
achievable ~0.93                  ->  should never have fired
```

So the no-fill was the system being SAVED by immediate-or-cancel, not a
malfunction. The defect is that it attempted at all.

**Fixed.**

- `KalshiMarket` now carries the real `yes_bid` / `no_bid` from
  `yes_bid_dollars` / `no_bid_dollars`, which the API published all along and
  the client never requested, plus a `bid(side)` accessor. A fabricated number
  can no longer masquerade as a quote.
- New `exit_slippage = 0.01`, the mirror of `entry_slippage`. The cash-out now
  judges itself on AND submits at `quoted_bid - exit_slippage`, so the order is
  marketable and a quote that is not really there declines instead of firing.
  At the live numbers 0.979 - 0.01 = 0.969 < 0.976: correctly declined.
- The message lied. Under the headline "CASH-OUT FAILED · STILL HOLDING" it
  printed "Banked +0.20 · 91% of the most this trade could make" - describing a
  sale that did not happen. A miss now reads "Nothing sold · +0.20 is what it
  WOULD have banked" and says the position is still fully exposed.

`tests/test_cash_out_bid.py` pins the exact live case: undiscounted it clears
the gate, discounted it declines, a genuinely rich bid still cashes out, and a
failed cash-out may not use the word "Banked".

**Still open.** The 2-second decision-to-submit gap is unexplained and
unfixed - the hourly move was the wrong suspect. The next thing to measure is
where the time actually goes between `now_ms` and the order, which means
timing `active_market` and `market.snapshot` directly rather than guessing
again.

---

## 25. Two gate sets, one message — and an inflated confidence count (2026-09-21)

KXBTC15M-26SEP211445-45 showed **five green ticks and "ENTRY READY"** and was
not traded. Both halves of that were defects.

### The alert and the auto path enforce different gates

The Checks block shows the RULE. The settle timer, the attempt caps and the
safety limits live only in the auto path, and nothing in the message said so:

```
14:37:59  auto[... DOWN@0.79 422s]: eligible
14:37:59  auto: declined - price has only held the band 0s, waiting for 120s
14:38:12  auto: declined - price has only held the band 13s, waiting for 120s
```

**It could never have fired.** The ask sat at 0.40-0.61 all window and entered
the band at **422s** remaining. 120s of hold completes at 302s; the entry
window closes at **360s**. Short by 58 seconds.

This is section 19's tension stated exactly: the entry window is 660-360s, a
300-second span, so **a price entering the band later than 480s remaining can
never satisfy a 120s settle**. That is a 2-minute dead zone at the end of every
entry window, and it is structural, not a tuning accident.

**Fixed (visibility only, no trading change):** the auto verdict is captured
and shown. A qualified setup automation refused now reads "Automation did NOT
take this - price has only held the band 0s, waiting for 120s" followed by
"Your press is the only thing that will." The button was always correct - the
manual stage exists to take what automation will not - but the reason for the
press was nowhere on screen.

### Confidence counted signals that could not disagree

The same message said **"BUY · confidence high (5/5 signals agree)"** on a
distance of **1.9x** the 5-minute move against a 1.5x floor - while the
evidence line directly beneath it called that gap **"moderate"**. A minute
later BTC was **83 cents** from the target and the market had flipped to 68%
the other way.

Two of the five were not independent:

| term | problem |
|---|---|
| `signed > 0` | **True by construction.** `model.predict` picks the side FROM the sign of the distance, so this scored on every entry ever alerted. |
| `vol_units >= 1.5` | Restated `min_normalized_distance`, already counted inside `rule_match`, and ticked identically at 1.9x and at 6x. |

So "5/5" was really about two independent signals - book and momentum - with
three points that were close to automatic. The count now holds four terms, the
free one is gone, and the distance term requires **3.0x**, the same boundary at
which the prose says "comfortable", so the number and the sentence under it can
no longer contradict each other.

Re-run on the exact live inputs: **MEDIUM, 3/4**, evidence "the gap is
moderate". Previously HIGH, 5/5. A setup wrong on book and momentum now scores
1/4 LOW, where the old count floored at 3/5 MEDIUM.

`decision_facts` feeds narration only - `action` is computed from `rule_match`
and the edge, not from `agreeing` - so this changes what the bot SAYS, not what
it trades. That is the right blast radius for a change made on one example.

**What is not fixed.** The dead zone above is a real constraint and the options
are all trading changes: widen the entry window past 360s, shorten the settle,
or accept that late band entries are manual-only. None is taken here, because
picking one off a single window is how the rules in section 14 got there in the
first place. What has changed is that the operator can now SEE which gate
refused, which is the prerequisite for measuring how often each one bites.

---

## 26. The dead zone fixed, the alert filled in, and hour-of-day re-tested (2026-09-21)

### The engine fix: settle 120s -> 60s

Section 25 named the dead zone and did not fix it. Fixed now, and chosen by
measurement rather than by picking one of three options.

`scripts/measure_window_settle.py` measures the entry window and the settle
timer TOGETHER, which neither section 16 (window alone) nor the
`band_streak_seconds` study (settle alone) had done. 71 days, one entry per
market, deployed gates, paired on markets:

```
 settle  lower      n  dead    won    net/ct                  95% CI     total
      0    360   5088     0  79.9%   +0.0060  [-0.0050, +0.0168]    +30.54
      1    360   3841  1247  83.6%   +0.0149  [+0.0032, +0.0260]    +57.42
      2    360   2681  2407  85.5%   +0.0147  [+0.0012, +0.0278]    +39.53  <- was deployed
```

And the test that decides it - the difference, bootstrapped directly:

```
settle 60s vs 120s   +0.0002/ct  [-0.0082, +0.0084]  p=0.479
trades gained        +1160 (+43%)
dead-zone setups     1160 fewer
```

**Indistinguishable per contract, 43% more trades, half the dead zone.** The
shorter settle buys volume without paying for it in quality. Dropping the
settle entirely (`settle=0`) fails to clear zero at every window bound, so the
rule stays - it is only half as long. **Widening the WINDOW instead is worse**
at every settle (300s / 240s / 180s all degrade), which is section 16 holding.
So the window did not move; only the timer did.

The live case that prompted it - KXBTC15M-26SEP211445-45, band entry at 422s -
now qualifies at ~362s, just inside the 360s cutoff.

### The alert now carries the numbers it was missing

A Context block sits under Checks:

```
Context
  · band settle: held 13s of 60s  waiting
  · measured edge: +0.0087/ct after a 0.0166 fee
  · distance: $85 from target
  · volatility: 5.2 bps / 5m
  · spread: 1.0 bps
  · session: us · 18:00 UTC
```

Settle is first because it decided almost every refusal on 2026-09-21 while
appearing in no message. The streak is now computed once, above the auto path,
so the alert and the refusal read the same number; `test_the_settle_requirement_is_enforced_before_an_order`
was updated to assert the ENFORCEMENT rather than where the call sits.

### Hour of day: re-tested as ONE pre-registered comparison, and still no

The operator's observation: the New York morning won on everything, and from
about 1pm local the losses started. 1pm local (UTC-4) is **17:00 UTC** - exactly
where section 4's weakest block begins, `US pm 17-20`, +0.0116 [-0.0096,
+0.0334]. Section 8 closed by asking for a single pre-registered re-test rather
than another re-slice. `scripts/measure_hour.py` is it:

```
17-20 UTC        n=852    +0.0069/ct
all other hours  n=4236   +0.0058/ct
DIFFERENCE       +0.0010/ct  [-0.0272, +0.0305]  p=0.542
```

**The afternoon is not worse.** It is marginally better on the point estimate
and the interval is wide through zero. In the 24-hour table exactly one hour
excludes zero - hour 10, -0.0661 [-0.1274, -0.0065] - which is what 24
independent tests produce by chance, and is not the hour anyone predicted.

**No gate added; the section 4 guard test stands.** What was done instead:

- **Tracking already existed** and was verified: `session`, `hour_utc`,
  `weekday` and `vol_regime` are written on every observation (2,075 of 2,087
  rows; the 12 nulls predate the instrumentation).
- **Registered forward** in `scripts/forward_test.py`, so the live record keeps
  measuring it. As of 2026-09-21: 17-20 UTC is 5/7, excess -0.1, z=-0.12; every
  other hour is 56/63, excess +7.1, z=+2.21. The pattern IS there today, on
  seven signals, which is why it is registered rather than traded.
- **Shown in the alert**, so a live regime can be seen and checked against the
  record instead of being inferred from a single afternoon.

The honest summary: today looks exactly as the operator describes, and 71 days
say it is not a property of the clock. Those are not in conflict - one day of
seven afternoon signals cannot distinguish a regime from a run, and the
registered test is the only thing that will.

---

## 27. Regime as a weight, not a gate (2026-09-21)

The operator's correction to section 26: hour-of-day should "add or reduce
weight, nor a blocker gate". That is a materially better proposal than the
filter section 4 forbade, and the reason is not diplomatic:

**A filter removes trades; a bounded multiplier cannot.** A filter fitted to
noise destroys real opportunity permanently and irreversibly. A weight floored
at 0.6 can only size some trades slightly wrong. Section 4's ban was reasoning
about removal, so it does not carry over to weighting - and the guard test has
been rewritten to assert the property that actually matters (**session can
never produce a refusal**) instead of the property it was asserting (session is
never mentioned near the order path).

**Weighting on noise is still a cost**, though - it adds variance with no
expected return - so the estimates are shrunk empirical-Bayes style:

```
shrink = tau^2 / (tau^2 + se_h^2)
tau^2  = var(observed hourly means) - mean(sampling variance),  floored at 0
```

Measured: **baseline +0.00601/ct, tau = 0.0140** against a typical hourly
**se = 0.0260**. So shrink lands around **0.21** and roughly **79% of every
hour's deviation is discarded as sampling error.** Raw hour 10 is -0.0661 and
shrinks to -0.0063; raw hour 14 is +0.0457 and shrinks to +0.0163.

Resulting weights span **x0.75 to x1.21**, none clamped:

| | |
|---|---|
| lowest | hour 10 UTC, **x0.75** - the only hour whose interval excludes zero |
| highest | hour 14 UTC, **x1.21** |
| operator's afternoon (17-20) | 1.03 / 0.96 / 1.16 / 0.89, averaging ~1.00 |

That last row is the check that matters: the pre-registered test of 17-20 UTC
found **+0.0010/ct [-0.0272, +0.0305], p=0.542**, and the weighting does not
quietly reintroduce the effect the test failed to find. It is pinned by
`test_the_operators_afternoon_is_not_singled_out`.

**The safety property is arithmetic, not opinion.** If every hour differs only
because each is noisily measured, the `tau^2` subtraction floors at zero, every
shrink factor is zero and every weight is exactly 1.0. Nothing has to be
switched off by hand. `test_an_hour_that_is_pure_noise_weighs_exactly_one`
pins it with a synthetic flat table.

**Stated plainly, because it would otherwise be oversold: at
`trade_contract_count = 1` this changes nothing.** Kalshi trades whole
contracts, so 1 x 0.75 and 1 x 1.21 both round to one contract, and
`contracts_for_budget` keeps its floor of one regardless. The weight scales the
BUDGET, so it becomes live in dollars only above roughly two contracts. It is
wired, visible in the alert's Context block, and measured - and it is currently
a no-op in money. `test_weighting_is_a_no_op_at_one_contract` records that so
the claim cannot drift.

**Not done, deliberately:** the weight is applied to the unattended path only.
The manual button still sizes from the operator's own budget, because the
press is their decision and silently resizing it would be a surprise.

---

## 28. THE PERMANENT RULE: time-of-day moves confidence, never the system (2026-09-21)

> Time-of-day may increase or reduce intelligence confidence, but it can never
> stop the 15-minute trading system.

Section 27 wired the regime lean into the order budget on a reading of "weight"
as position size. **That reading was wrong and was never asked for. Position
size belongs to the operator alone and stays at one contract.** Reverted the
same day; `regime.py` no longer contains any function that accepts a budget, so
reaching for one again means writing it rather than calling it.

### What regime IS allowed to do

Exactly one thing: state the confidence as arithmetic.

```
Base confidence:        HIGH
14:00 UTC adjustment:   +21
Adjusted confidence:    HIGH

Base confidence:        HIGH
10:00 UTC adjustment:   -25
Adjusted confidence:    MEDIUM
```

Confidence is now scored out of 100 - 4 agreeing signals = 100, 3 = 70, 2 = 50,
1 = 25 - with HIGH at 85 and MEDIUM at 40, which preserves every existing
verdict exactly. The regime lean converts to points as
`round((weight - 1) * 100)`, capped at +-25, so it can move a label but never
erase one. Shown in the narration as its own line, so a moved label is never
mistaken for the signals having changed.

### What it must never do, each one pinned by a test

| prohibition | test |
|---|---|
| skip a 15-minute market | `test_it_never_skips_a_market_or_stops_polling` |
| stop polling | same |
| prevent the strategy evaluating | `test_it_never_prevents_the_strategy_from_evaluating` |
| block an otherwise qualified order | `test_it_never_blocks_an_otherwise_qualified_order` |
| disable alerts or archiving | `test_it_never_disables_alerts_or_archiving` |
| **touch position size** | `test_it_never_touches_position_size` |

The order path asserts `contracts_for_budget(limits.budget, contract_ask)`
verbatim and that no regime symbol appears in the refusal logic;
`strategy.py`'s `matches` is asserted to contain no notion of hour, session or
regime at all. A comment is not a test, and this one was already violated once.

### Every window is recorded through settlement

`observations` now carries `regime_weight` and `confidence_adjustment`
alongside the existing `session`, `hour_utc`, `weekday`, `vol_regime` and the
settled `won`. Archiving is unconditional and the regime is written INTO it
rather than around it. Confirmed live at 19:13 UTC: `hour=19 us weight=1.163
adj=+16`, recorded on a signal that did not qualify - because recording does
not depend on qualifying.

### The estimates stay honest on their own

The lean is shrunk empirical-Bayes by how uncertain each hour is
(`tau = 0.0140` against a typical `se = 0.0260`, so ~79% of every hour's
deviation is discarded). If hours ever differ only by sampling error, `tau^2`
floors at zero, every adjustment becomes exactly 0 and every label is the base
label - **no opinion required, the arithmetic switches it off.** Pinned by
`test_an_hour_that_is_pure_noise_weighs_exactly_one`.

And the operator's own hypothesis is held to the same standard: 17-20 UTC
measured +0.0010/ct [-0.0272, +0.0305], p=0.542 against every other hour, and
`test_the_operators_afternoon_is_not_singled_out` asserts that block averages
~1.00 so the weighting cannot quietly reintroduce an effect the pre-registered
test failed to find. Its live adjustments are +3, -4, +16, -11.

---

## 29. The similarity layer, and two things that should never have been gates (2026-09-21)

### Enter now or wait? Measured first, because nobody had

`scripts/measure_wait.py`, 71 days, one entry per market:

```
a cheaper ask appeared later    1337 (26%)
it only got dearer              2548 (50%)
mean ask drift, first to last   +0.0531   (5.3c DEARER)

now       +0.0060 [-0.0050, +0.0168]   +30.54   <- deployed
patient   +0.0202 [+0.0093, +0.0308]  +102.71   <- ORACLE, needs foresight
last      -0.0301 [-0.0407, -0.0203]  -153.40
late      -0.0116 [-0.0234, +0.0005]   -42.87

last - now   -0.0362 [-0.0413, -0.0308]  WORSE
late - now   -0.0176 [-0.0303, -0.0050]  WORSE
```

Every implementable waiting policy is significantly worse. Only the oracle,
allowed to pick the cheapest ask the window ever showed, beats entering now.

**The operator's correction, which was right:** a late ask of ~95c is not an
artifact to argue away. A 15-minute contract decays toward its outcome, so a
late price is simply the price after the information has arrived. It happens
regardless, and that is exactly WHY waiting is expensive - you end up buying
certainty you could have bought cheaply.

### The similarity layer

`src/btc15_signal/similar.py` plus a 74,582-row corpus over 6,428 settled
markets (`scripts/build_cohort.py`). At every 15-minute setup it fingerprints
the market, retrieves ~60 comparable settled lifecycles and compares three real
policies at OUR price:

```
ENTER NOW              posterior - ask - fee
WAIT FOR RETRACEMENT   rest a limit below the ask; fill in P(dip), else NO TRADE
PASS                   neither pays
```

Two rules built in, both the operator's:

1. **Win rate never decides.** Every verdict is net edge. 89% at 93c is a PASS.
2. **A thin cohort is not evidence.** The win probability is shrunk toward the
   corpus base rate (0.789) by cohort size.

The "no fill = no trade" term is the part that matters: waiting is not free
optionality, and its cost is the setups that never come back.

First live read, 15:34 UTC:

```
KXBTC15M-26SEP211545-45 UP ask=0.83 cohort=60 p(win)=0.86 (raw 0.88) edge=+1.57c
now=+0.0157  wait_limit=-0.0109 @ 0.80 (fills 57%)  drift=+0.104
ACTION=ENTER NOW
```

**SHADOW ONLY.** Recorded in `shadow_decisions`, settled with the window,
graded by `scripts/score_shadow.py`. Promotion requires its win probability to
be calibrated AND its disagreements with the deployed rule to be paying.

### Two things that should never have been gates

**The level.** Deployed as a gate in section 23 on the operator's instruction,
then corrected by them: it "should have not become a block, it was supposed to
be a confidence helper like the time of the day". They are right, and the
evidence always said so - the difference between having a blocking level and
not was +0.0140 [-0.0067, +0.0344], p=0.090, and the gate was refusing about
half of all qualifying setups on that. `require_blocking_level` is now false
and the level contributes CONFIDENCE POINTS instead, shrunk by its own
uncertainty exactly as the clock is:

```
shrink = 1 - se^2/effect^2 = 0.429
level in the way  +6 points
no level          -6 points
```

It appears in Checks only when it actually gates. A green tick must never
imply something had a say when it did not.

**Confidence is now one line of arithmetic** in the alert:

```
confidence: base HIGH (4/4) · clock +16 · level -6 · adjusted HIGH
```

### One message, not two

The separate LLM commentary message repeated what the alert already carried -
checks, context, confidence, similar-regime read - so
`brain_commentary_enabled` defaults to false.

### Everything that supported an order travels with it

`decision_records` captures the gates, settle, edge, distance, volatility,
momentum, spread, session, regime lean, level, fill and cohort read against the
proposal id, and the fill message prints it as **WHY THIS TRADE**. Those facts
were previously scattered across four tables joined on drifting timestamps, so
"why was this trade taken" was a reconstruction rather than a record.

---

## 30. Five corrections to the similarity layer (2026-09-21)

An operator review of section 29 raised five points. Three were correctness
bugs, one was already right, one was a naming error that made a correct sign
look backwards. All verified against the code rather than assumed.

### 1. Deduplication — WAS A BUG, fixed

The corpus holds 74,582 decision minutes across 6,428 markets, and the query
window spans +-2 minutes, so a nearest-60 could return five minutes of the same
lifecycle and count them as five pieces of evidence. Measured before the fix:

```
ask=0.92 nd=1.6 rem=660 : 60 rows -> 51 unique markets
worst case              : 9 duplicate rows in 60
```

**The bias is not random.** A market that sat in the band for many minutes is
disproportionately one that went on to win, so duplicates inflate the win rate
in the flattering direction. `neighbours()` now keeps only the closest row per
ticker: `n` is always unique lifecycles. Verified: 60 rows, 60 unique.

### 2. Conditional wait probability — ALREADY CORRECT

The review asked that `EV(wait)` use `P(win | dipped and filled)` rather than
reusing the enter-now probability, because a dip may carry adverse information.
It already did:

```python
dipped = [r for r in rows if r["best_later_ask"] <= dip_price]
dip_posterior = (dip_wins + PRIOR * base) / (len(dipped) + PRIOR)
wait_limit = dip_rate * (dip_posterior - dip_price - dip_fee)
```

The posterior is rebuilt from the dipped subset alone, and shrunk on that
subset's own size. Left unchanged.

### 3. Walk-forward isolation — WAS UNENFORCED, fixed

Nothing stopped a comparable market that settled AFTER the decision from
informing it. Live this was vacuous today - the corpus ends 2026-09-20 and
every read is later - but it stops being vacuous the moment live markets join
the corpus or a historical decision is replayed, and a leak found then would
invalidate every number measured in between. `read()` and `neighbours()` now
take `as_of_ms` and admit only markets settled by `open_ms + 900_000 <= as_of`.
The live path passes `now_ms`. Proven to bite:

```
as_of 2026-07-14 (corpus start):   0 neighbours
as_of 2026-07-19:                 51 neighbours
as_of 2026-08-13:                 60 neighbours
```

### 4. Decisions without proposals — WAS A BUG, fixed

`decision_records` was keyed on `proposal_id`, so the archive held only the
decisions that became orders. PASS, WAIT, DECLINED and unfilled are the
counterfactuals and are arguably the more valuable half. Now keyed on
**observation_id**, with `signal_id`, `market_id`, `strategy_version` and an
OPTIONAL `proposal_id`, and written on the decline path too. First live row:

```
obs=1790020800000:576  sig=dc016f3d3daf5820  mkt=KXBTC15M-26SEP211615-15
strategy=dba3e62c15  action=DECLINED  proposal=None
reason=price has only held the band 0s, waiting for 60s
ask=0.77  shield=None  level_adj=-6  cohort=60
```

`strategy_version` is a digest of the rule file plus the settle timer, entry
window and slippage - without it the archive silently averages decisions taken
under different rules, and 2026-09-21 alone would have pooled four bands and
two settle timers.

### 5. The level sign — NAMING ERROR, fixed

The semantics were right and the name was not. `blocking_level` read as "an
obstacle to our move", which makes `+6` look backwards. It is the opposite: a
level standing between BTC and the target, blocking the move that would BEAT
us. Renamed **protective_level** throughout, displayed as "resistance $85,845
shields the target" / "nothing shielding the target", and the confidence line
now reads `shield -6`. The old name survives as an alias so a rename cannot
break a caller.

### Uncertainty is now displayed

"86% from 60 markets" reads as a fact and is not one:

```
Comparable unique markets: 60
Estimated win probability: 89% (raw 93%, base 79%)
95% interval: [84%, 97%] - 60 markets settles nothing
Net edge after costs: -3.43c
Shadow preference: PASS
```

The review's own point stands and is now on the face of the message: a 1.57c
edge from 60 neighbours is nowhere near established. The layer remains
SHADOW-ONLY until `scripts/score_shadow.py` shows its probabilities calibrated
and its disagreements paying.

---

## 31. Kalshi market structure: fees, queue, and where the volume is (2026-09-21)

Four external facts about the exchange, established from live order tickets and
from the recorded book. None of them is in the code's assumptions, and three of
them change how a fill should be read.

### Maker fills are free. Taker fills are not.

From the operator's own order tickets, at ordinary mid-book prices:

| book | limit | crosses? | fee |
|---|---|---|---|
| bid 60c / ask 61c | **59c** (below bid) | rests - **maker** | **$0** |
| bid 40c / ask 41c | **38c** (below bid) | rests - **maker** | **$0** |
| bid 35c / ask 36c | **38c** (above ask) | crosses - taker | $0.02 |
| bid 65c / ask 66c | **66c** (at ask) | crosses - taker | $0.02 |

`0.07 x 0.36 x 0.64 = $0.016 -> $0.02` confirms `kalshi_fee_charged` is exactly
right for takers. **It models no maker case at all**, so it overstates the cost
of any maker fill by the whole fee.

The size of that matters: the fee is ~1.3-1.7c at the prices this bot trades,
against a measured edge of ~1.5c. **Eliminating it would come close to doubling
the edge** - the single largest improvement available anywhere in the system.

**The app displays profit NET of the fee it already charged.** A position bought
at 88.7c and marked at a 96.4c bid showed `+$0.07`: 7.70c gross minus the 0.71c
entry fee already paid. Our model adds the fee to cost instead; same number from
the opposite side. No double-count, but any comparison between a screenshot and
a computed figure has to know which convention it is reading.

### The maker rebate is real and unreachable at one contract

Contracts already resting at our own best bid, over the recorded books in the
0.70-0.93 band (n=1,586 snapshots):

```
p10          215
median     3,989
p90        7,640
max       44,614

queue <=   50 contracts :  5% of the time
queue >= 1000 contracts : 81% of the time
```

A 1-lot joining a ~4,000-deep queue fills only once roughly 4,000 contracts
trade through that price - that is, when the level is **swept**. A swept level
is exactly the adverse case, and the adverse case is expensive:

```
discount   filled  win|filled   delta vs band   net (no fee)
   1c        54%      68.9%        -9.6%          -0.0713
   3c        46%      65.9%       -12.7%          -0.0804
   5c        41%      63.1%       -15.5%          -0.0869
```

**The fee saving is ~1.3c; the adverse selection is ~10c.** Two measurements
bracket the truth by the queue assumption, and the depth data says which end
applies:

- fills on any touch (front of queue): **+0.0213/contract** - requires a
  position in the queue a 1-lot does not have
- fills only when the level is swept (back of queue): **-0.0713/contract**

Against **+0.0003** for simply crossing. Do not rest entries below the market.

### The volume is where the edge is not

Per-minute traded volume against the edge at ask >= 0.90, by minutes remaining:

| min left | median vol/min | edge |
|---|---|---|
| 11 | 0 | +0.0208 (n=201) |
| 7 | 1,490 | +0.0101 (n=1,589) |
| 6 | 1,144 | +0.0100 (n=2,203) |
| 5 | 4,444 | +0.0021 |
| 3 | 8,828 | -0.0019 |
| 2 | 11,802 | **-0.0084** (n=4,391) |
| 1 | 40,289 | -0.0039 |

**Volume rises 30x into the close and the edge crosses to negative on the way.**
The crowd buying 90c contracts in the last two minutes for a net 5-8% is, in
aggregate, paying for the privilege.

This reframes the fill problem. **The bot trades in the illiquid part of the
window by design**, because that is where the edge is: at 6-11 minutes the
median traded volume is 0-1,500 per minute. The book shows a quote and almost
nothing changes hands. A low fill rate is the PRICE of trading where the edge
is, not a malfunction - and the obvious fix, trading later, destroys the thing
being protected: **+0.0100 at 6 minutes against -0.0084 at 2**.

### How fast the ask moves, and what an allowance has to cover

Signed ask drift across the measured ~1.93s decision-to-submit lag, at
qualifying polls (n=423):

```
p50 +0.00c   p90 +1.10c   p95 +1.59c   p99 +2.72c   max +4.13c

1c allowance covers 89.1%    3c covers 99.1%    5c covers 100.0%
```

The 1c allowance that was deployed was therefore under-priced on about one
qualifying order in nine - and every one of those returned "no fill; the book
moved". This is what motivated pricing the entry at a ceiling rather than at
`ask + slippage` (the cross-to-ceiling change, committed as cb1230d).

Qualifying moments are NOT the fast ones, which was worth checking and is the
opposite of the intuition: 11% of qualifying polls exceed a 1c allowance
against 15% of all polls.

### Section 7's order-book mapping: narrowed, not closed

The structure is now identified:

```
max(book_yes)      = yes_bid
1 - max(book_no)   = yes_ask
```

Median error **-0.10c** across 4,115 recorded snapshots, which is the right
answer. But only **22%** agree within 1c, tails run **+-12c**, and the error
gets WORSE with more time remaining (12% within 1c at 660s, 42% at 60s) -
the opposite of what staleness would do, and the book/quote capture skew is
only ~338ms. Worst cases are structural, not drift: `quote_bid 0.380` against
`book_best 0.994`.

Two uneliminated candidates, both in `recorder.py`: the `depth: 32` request may
return a window that is not centred on the touch, and `live[0]` selects a
market without the "exactly one open market" guard the trading path enforces.

**Consequence:** queue position cannot be simulated from the recorded book, so
the maker question above cannot be narrowed further by analysis. It would need
a live experiment - rest one contract at the bid, record fill rate, fill price
and outcome - and the live path now carries Kalshi's authoritative bid
(section 24) to do it with.

---

## 32. Loss recovery, and where the edge actually lives on the distance gate (2026-09-21)

### The martingale recovery overlay: measured, and not adopted

The operator's proposal: after a loss, arm a recovery leg that fires on the
next window meeting every normal gate at 0.90-0.93 with about two minutes left,
stakes ten contracts to cover the loss, then disarms.

`scripts/measure_recovery.py`, 6,435 settled markets:

```
normal system   n=5088  +30.54   (1025 losses)
recovery        n=488   +2.84    (92.0% won, 39 losses, worst -9.35)
per trade       +0.0058  95% CI [-0.2394, +0.2418]
max drawdown    -76.17   against -26.91 without the overlay
```

It wins 92.0% of the time and makes nothing. The break-even is the price:

```
at 0.90: win +0.94, lose -9.06 -> needs 90.6%
at 0.92: win +0.75, lose -9.25 -> needs 92.5%
at 0.93: win +0.65, lose -9.35 -> needs 93.5%
```

One loss erases eleven wins, and the overlay bought an indistinguishable-from-
zero gain for **three times the drawdown**.

**The live record agrees, and more sharply.** Replaying the deployed
configuration over the live signals: 7 fires, 3 losses, **-24.07**. Seven fires
settle nothing on their own, but the sign matches the drawdown.

### The sweep found a "sweet spot" that is really "stop trading late"

`scripts/optimize_recovery.py` over 108 configurations. Per contract, with size
removed - size is leverage, not skill, and 20 contracts earns exactly twice the
10-contract edge:

```
1-2 min  n=628  93.9%  -0.0057/contract
1-3 min  n=792  95.1%  +0.0069
2-4 min  n=841  95.0%  +0.0102
3-5 min  n=834  94.8%  +0.0110
4-6 min  n=798  95.2%  +0.0170
```

Monotone. The optimiser wants to move the recovery leg AWAY from two minutes
and back toward the normal entry window - section 31 again, where the edge
declines into the close as volume rises. **The recovery idea's whole premium is
on late entry, and late entry is the worst part of the window.**

Only 3 of 108 cells clear zero against ~2.7 expected by chance. A shuffle
control put the best real cell beyond the null (p~0.005), but that null is
i.i.d. and so destroys loss CLUSTERING - and the overlay fires after losses, so
clustered losses beat an i.i.d. null for reasons that have nothing to do with
the parameters. Not adopted.

**A control that was wrong first time.** The initial null drew `won =
random() < ask` - each trade winning at its own price. This strategy exists
because these markets win MORE than their price (section 1), so the real data
beat that null regardless of parameters: it tested "is there any edge at all",
not "did the search find one". Replacing it with a price-conditional empirical
rate moved the null's median from +106 to +175. Most of the apparent signal was
the base edge.

### The distance gate is a BAND, not a floor - and the confidence score had it backwards

Measured on 3,841 deployed entries (settle 60s), momentum aligned:

| distance | n | won | net/contract |
|---|---|---|---|
| 1.5-2x | 292 | 80.5% | -0.0012 |
| **2-3x** | 679 | 84.8% | **+0.0360** |
| 3-5x | 1143 | 84.1% | +0.0195 |
| 5x+ | 1727 | 83.3% | +0.0064 |

**The edge peaks at 2-4x and decays above it.** A strike far enough away to be
safe is already priced for the safety it offers, so paying up for distance buys
nothing. `regime.py`'s confidence score awarded its distance point for
`>= 3.0x` - "more is better" - which scored a 13x setup as confidently as a 3x
one and scored the best bucket of all as no better than the floor. Corrected to
the measured `2.0 <= x < 4.0` band in `decision.py` and in the alert.

**One condition separates on its own**, and it is not distance:

```
momentum aligned   n=3638  84.2%  +0.0197  [+0.0078, +0.0315]
momentum against   n=203   71.4%  -0.0701  [-0.1329, -0.0074]
```

### What was adopted: confidence sizing

Flat $2 was measured first and scales exactly: +115.03 against +57.42, and
-37.05 drawdown against -18.53. Same trades, same win rate, twice of both.
Recovery time is unchanged and is worth knowing - **median 11 trades to repay a
loss, p90 101, worst 1,086**.

Deployed instead is size conditioned on the measured band:

```
all entries        +0.0149/ct  [+0.0032, +0.0260]
2.0-4.0x aligned   +0.0359/ct  [+0.0166, +0.0541]   n=1282, 33% of entries
everything else    +0.0157/ct  [+0.0023, +0.0286]
```

Two contracts inside the band, one everywhere else - 2.4x the edge on a third
of trades, and no extra size on the setups that do not support it. The
difference against the rest does NOT clear zero on its own
(+0.0226 [-0.0068, +0.0523]); what carries this is that the band's own interval
excludes zero and is more than twice the pooled edge.

`auto_daily_loss_limit` raised 10 -> 20 alongside it: a 2-contract loss is
about $1.87 against $0.93, so the old floor tripped after half as many bad
trades. A floor that stops a normal losing run early is a silent stop, not a
safety control.

## 33. The out-of-the-money strategy: the entry is the problem, the take-profit makes it worse (2026-09-21)

Operator specification: from the START of the window, buy whichever side is
available at about a 35% chance, set a take-profit at 150% of what was paid,
and watch both sides for the same opportunity. Measured by
`scripts/measure_oom.py` over 6,435 settled markets, 4,876 trades, both sides
taken independently.

| Fee model | n | TP hit | held won | per trade | 95% CI | ROI |
|---|---:|---:|---:|---:|---:|---:|
| taker entry, **maker** take-profit (free) | 4876 | 45.3% | 3.5% | -$0.0402 | -0.0488 to -0.0310 | -12.3% |
| taker both ways | 4876 | 45.3% | 3.5% | -$0.0472 | -0.0557 to -0.0382 | -14.5% |
| UP side only | 2462 | 45.0% | 4.4% | -$0.0371 | -0.0503 to -0.0238 | -11.4% |
| DOWN side only | 2414 | 45.6% | 2.6% | -$0.0433 | -0.0564 to -0.0298 | -13.3% |

Both intervals sit entirely below zero, and both sides lose independently, so
this is not one side's spread. The free maker exit does not rescue it.

**The premise is true and it does not help.** 45.3% of these contracts DO reach
1.5x - cheap contracts really do come back. But of the 2,209 that hit the
take-profit, **1,428 (64.6%) would have settled as winners worth $1.00** and
were sold at ~$0.62. Of the 2,667 that never reached it, only **93 (3.5%)** went
on to win. The take-profit sells the winners and keeps the losers:

    given up by selling early      -$513.22
    saved by selling before expiry +$460.34
    net                             -$52.88

Held to expiry the same entries lose **-$0.0293**/trade; with the take-profit
they lose **-$0.0402**. The difference is **-$0.0108**/trade, 95% CI
[-0.0195, -0.0022] - an interval clearing zero on the negative side, so the
take-profit is reliably harmful rather than merely useless.

**Why the entry itself has no edge.** 1,521 of 4,876 settle as winners = 31.2%,
bought at an average of ~$0.325. That is the far side of the favourite-longshot
bias in section 1: favourites win MORE than their price, so longshots win LESS
than theirs. Buying the 35% side is taking the wrong end of the only durable
bias this market has, and the fee finishes it.

This independently reproduces the deployed spike-reversion result
(`reversion_strategy.json`, `enabled: false`): -$0.0440/contract over 760
trades, 95% CI [-0.0609, -0.0277]. Two differently-constructed tests of the
same idea - one with a spike/rejection setup and a fixed 0.50 exit, one with no
setup at all and a 1.5x exit - land within half a cent of each other. The
agreement is the finding; the parameters are not what is wrong.

**Decision: not deployed.** No take-profit level is worth searching, because
the diagnosis is structural rather than parametric - the entry is on the wrong
side of the bias, and any exit rule that helps the losers cuts the winners by
more. What would change this: an entry filter that predicts WHICH cheap
contracts come back, tested walk-forward, and section 7 applies in full to any
such filter that a search finds.

## 34. Realised P&L was rebuilt locally and had the sign wrong (2026-09-22)

Telegram reported **`Live: +1.06`** on an account that was actually **down
$1.62**, over **47** of the **88** markets that had really settled. The figure
was reconstructed from `trade_proposals` rather than read from the exchange,
and it was wrong in four independent ways:

| Cause | Effect |
|---|---|
| `COALESCE(fill_price, entry_limit)` | a limit is permission to cross, never the price paid - our own fills went limit 0.87 / filled 0.84 |
| fee computed by `kalshi_fee_charged` | a faithful copy of the formula is still not the debit Kalshi applied |
| settlement taken from `predictions.won` | our own guess at the result, not the exchange's |
| `ACCOUNTED_SQL` excludes `pending` | a proposal that FILLS but never reaches a terminal status stays `pending` for ever - 71 such rows on 2026-09-21 alone, and 41 settled markets missing overall |

**The fix: read `/portfolio/settlements` and never model money again.** Mirrored
into a `settlements` table once a minute on the same beat as the settlement
sweep, so it survives an outage and a report never waits on the network.

**The trap, which cost the most time to find: `revenue` is not the payout.**
Closing a position early is booked as BUYING THE OPPOSITE SIDE, and Kalshi nets
the offsetting pair at $1 immediately - outside `revenue`, which then reads 0 on
a market that won. `KXBTC15M-26SEP220615-15`: 2 NO bought for $1.60, closed by
buying 2 YES for $0.012, `market_result` "no", `revenue` **0**. Scoring that by
`revenue` books a $1.63 loss on a trade that made **+$0.365**. The correct
payout is the netted pairs PLUS whatever actually settled:

    payout = min(yes_count, no_count) * $1.00 + revenue/100
    pnl    = payout - yes_total_cost - no_total_cost - fee_cost

Verified against the live account: 88 settlements, API total and mirror total
agree to **0.000000**.

**The daily loss floor deliberately does NOT switch over.** It now takes the
MORE NEGATIVE of the exchange figure and the old local reconstruction. A floor
must never be relaxed by a source that can be stale or empty - if the mirror has
not synced, reading it alone returns 0.00 and silently disables the floor on a
day that has already lost money. It can be early to stop, never late.

The cash-out message also stopped modelling its fees: `fill_detail` was already
returning the charged amount and discarding it, and the entry fee was on
`trade_proposals.fee_paid` the whole time.

Covered by `tests/test_exchange_pnl.py`, which pins the netting case with the
real API row. `scripts/sync_settlements.py` backfills and verifies.

**Still open:** the `pending` status leak itself. Marking expired proposals
`expired` would make "what did the filter actually take?" answerable - section
33's filtering question currently rests on 17 identifiable trades out of 88.

### 34a. The correction: right formula, wrong scope (2026-09-22)

The fix above was verified against the API and still reported a number the
operator could see was wrong. `Live: -1.40` went out while the Kalshi app
showed **+$4.05 (+14.67%)**. The formula was correct; two things around it were
not.

**It was reporting ALL TIME. The app reports TODAY.** Every settlement since
the account opened was being summed into a line printed beside a screen showing
one day. The two are not close and never will be:

| Market day (`window_ms`) | Markets | P&L |
|---|---:|---:|
| 2026-09-19 | 5 | -2.6753 |
| 2026-09-20 | 30 | -2.6569 |
| 2026-09-21 | 37 | +0.0022 |
| 2026-09-22 | 18 | **+4.0734** |
| all time | 90 | -1.2566 |

**It was bucketing by SETTLEMENT time, which is not the market's time.** Kalshi
settles in batches hours after close - `KXBTC15M-26SEP220445-45` settled at
08:45 UTC, four hours after its window. Grouped by `settled_time`, 2026-09-21
read **-3.3256**; grouped by the market's own time, parsed from the ticker, the
same day reads **+0.0022**. The daily loss floor was being handed a window that
mixed one day's trades with another's.

**And the app's headline is not realised alone.** It is today's realised P&L
PLUS the open position marked to the bid. Reproducing it needs both halves:

    realised today (18 settled)          +3.9251
    open position marked at the bid      +0.1415
    total                                +4.0666
    the app, six minutes earlier         +4.0500

The 1.7c gap is the bid moving between the screenshot and the query.

**Also corrected: the count.** Executed trades now come from
`/portfolio/fills` - 159 of them across 90 markets - because counting
`trade_proposals` counts intentions, misses anything filled outside the bot and
miscounts anything stuck at `pending`.

**And the dashboard was left quoting its own total.** It now prefers the same
mirror, because two surfaces reporting different P&L for one account is the
failure this whole path was rewritten to end.

The lesson worth keeping: *agreeing with the API is not the same as being
right.* Both numbers were faithfully computed from Kalshi's own fields. The
error was in what question they answered, and only the operator's screen
exposed it.

## 35. The Telegram rewrite, and the two numbers that disagreed (2026-09-22)

Operator specification: keep all four checks visible on entry-ready signals,
executed orders AND rule-rejected signals; move only the extended context and
the similar-regime read behind a DETAILS button; separate the trade OUTCOME
from the predicted DIRECTION in results; and - the requirement that found a
real defect - **every displayed check must come from the same decision
snapshot**.

**The contradiction was real.** Momentum was computed in two places with two
conventions. `EntryRule.check_detail` signed it FOR OUR SIDE - a DOWN bet with
the market falling scores +3.3 bps, because the market is moving our way -
while `entry_context` printed the RAW market figure, -3.3 bps. The same alert
carried both, three lines apart, with nothing to say which one the rule had
used. `EntryRule.check_facts` is now the single source: `check_detail` derives
from it, the alert renders from it, the fill report renders from it, and the
confidence label is scored from it. Pinned by `tests/test_one_snapshot.py`.

**A refusal now says by how much it missed.** The old paper alert printed only
the NAMES of the failed gates - "model confidence, contract price band, target
distance, momentum strength" - which states that a setup was refused four times
without stating how close any of them came. Each fact now carries its own pass
and fail wording, so `0.8x volatility - needs 1.5x` replaces `target distance`.

**Results state the money and the call separately**, because they come apart.
A DOWN position sold at 100c on a market that later settled UP is a PROFIT and
a WRONG PREDICTION simultaneously. Headlining by the call books a loss the
account never took; headlining by the money alone hides that the signal was
wrong. Both are stated, with their own ticks.

**A defect introduced and caught in the same pass:** with `pnl=None` the new
layout rendered `LOSS - $0.00` under a red chip, asserting a loss on money that
had simply not been computed. It now reports the CALL and says the money is
unsettled - the same class of error as section 34, where a figure nobody had
measured was presented as fact.

Kept, though the specification did not show them: the rule-qualified marker on
untraded signals (a signal the rule approved and nobody pressed is a trade that
got away; one it refused is not), and the unconfirmed-fill warning (a limit is
permission to cross, never the price paid).

The DETAILS button replays text frozen at decision time rather than recomputing
it, because re-deriving the context when the button is pressed would describe a
market that has already moved - and the audit trail is the whole point.

## 36. The intelligence investigation: two models, neither beats the price (2026-09-22)

Built the walk-forward, regime-conditioned pre-trade layer to specification and
measured it. Two results decide the promotion question, and both are negative.

### Neither model beats the market

Expanding-window walk-forward over `data/cohort.db`, 6,428 markets deduplicated
to one row each, 5 folds:

| model | n | Brier | LogLoss |
|---|---:|---:|---:|
| logistic baseline (`baseline.py`, 8 features) | 5,355 | 0.2114 | 0.6111 |
| **the market price itself** | 5,355 | **0.2112** | **0.6106** |
| cohort nearest-neighbour (`similar.py`) | 5,278 | 0.2133 | 0.6159 |

**The ask is the best available estimate of the outcome.** Neither the
retrieval layer nor the logistic regression improves on simply reading the
price, and the retrieval layer is the worst of the three. This is what section
1's favourite-longshot finding predicts: these markets are priced well, they
merely win slightly MORE than their price, and that residual is a property of
the price rather than something a model adds to it.

The spec's selection rule - prefer the simplest model when performance is
indistinguishable - therefore does not even reach the tie-break. A 6,428-market
retrieval layer is not beating eight coefficients, and eight coefficients are
not beating one number that is already on the screen.

Calibration, where it has data, is genuinely good:

    predicted 0.54 -> observed 0.56  (n=1594)
    predicted 0.69 -> observed 0.69  (n=3025)
    predicted 0.85 -> observed 0.83  (n=658)

So the layer is honest about its own uncertainty. It simply has no information
the price does not already carry.

### The live shadow archive is 95% corrupt

`record_shadow_decision` wrote `VALUES (?,?,...)` positionally against a
hand-counted width. When `dip_n` was appended to the live table by a later
migration the tuple order stopped matching the column order, and every value
from that point shifted one place: `session` holds a probability, `dip_n` holds
the volatility regime, `win_low` holds a session.

**93 of 98 rows are affected.** It stayed invisible because `settle_shadow`
updates `won` BY NAME after settlement, so the one column anybody checks was
being silently repaired while the rest stayed wrong.

This is the third time this exact pattern has cost something: the same blind
positional edit put 30 values into the 24-column observations insert on
2026-09-21 and broke `observe()` outright. The insert is now by NAME, and
`tests/test_intelligence.py` adds a column mid-test to prove a migration cannot
shift the others.

Five usable rows remain. Three of those are executable ENTER NOW decisions, at
-0.0962/contract with a 95% interval of [-0.6855, +0.2368]. That is not
evidence of anything, and it is not supposed to look like it.

### Verdict: REMAIN SHADOW

Blocking, in order of how much work each needs:

1. no model beats the market price out of sample - the finding, not a data gap
2. the live archive cannot support a claim until it refills post-fix
3. 3 settled executable decisions against a 200-decision floor

The honest reading is that item 1 is not waiting on more data. A layer whose
best case is matching the ask has nothing to contribute to a decision the ask
is already making. What WOULD change the answer is a feature the price does not
see - execution-side information, book dynamics in the seconds before a fill -
rather than more of the market state the price already reflects.

## 37. Execution timing: the dip is information, not a discount (2026-09-22)

Section 36 closed the direction question - no model beats the ask. The
operator's next hypothesis was the right one to test: if there is an edge, it
is in execution timing, fill behaviour and retracement rather than in another
opinion about UP or DOWN.

Measured PAIRED, enter-now against waiting ON THE SAME MARKET, so whether that
market won or lost is held constant and cancels. 6,428 markets, one row each.
Pairing is what makes this answerable at all: it removes the direction variance
that swamped sections 7 and 33, and resolves a 1c timing effect on a sample
where an unpaired test would need hundreds of thousands of trades.

| policy | n | fill | mean vs enter-now | 95% CI | t |
|---|---:|---:|---:|---:|---:|
| wait-limit 1% below | 6428 | 74% | **-0.0700** | -0.0737 to -0.0663 | -37.4 |
| wait-limit 2% below | 6428 | 72% | **-0.0697** | -0.0737 to -0.0657 | -35.2 |
| wait-limit 3% below | 6428 | 70% | -0.0689 | -0.0732 to -0.0650 | -33.3 |
| wait-limit 5% below | 6428 | 66% | -0.0674 | -0.0720 to -0.0630 | -29.9 |
| wait-to-last quote | 6428 | 100% | -0.0005 | -0.0107 to +0.0095 | -0.1 |
| wait-ORACLE (needs foresight) | 6428 | -- | +0.2484 | | |

**Every implementable wait policy loses, by seven cents, at t = -30 to -37.**
Not a marginal result and not a sample-size problem.

### Why, which matters more than the verdict

| group | n | win rate | avg ask |
|---|---:|---:|---:|
| limit FILLED - the ask dipped 2c | 4,610 | **54.1%** | 0.65 |
| limit MISSED - the ask never dipped | 1,818 | **99.3%** | 0.68 |
| all markets | 6,428 | 66.9% | 0.66 |

A two-cent dip costs **45.3 percentage points of win rate**. You save 2c on the
price and give up 45c of expected payout: **-0.4328/contract before fees**.

The dip is not a discount. It is the market telling you the trade is going
wrong, and a resting buy fills PRECISELY when you would rather it had not -
the same adverse selection that killed maker orders in section 31, now
measured directly rather than inferred from queue depth.

This also explains why a model cannot rescue it. Retracement is highly
predictable - the walk-forward logistic calls it correctly 72.0% of 5,355
out-of-sample markets - and selecting on it changes nothing: model-selected
waiting scores +0.0000 [-0.0000, +0.0001] against always waiting. Predicting
the dip is easy; the dip is simply not worth having.

### What this validates, and where the edge actually is

It vindicates the deployed execution path. Crossing immediately to the ceiling
(section 34's entry logic) is not a compromise forced by latency - it is
correct on the measurement. Waiting for a better price is a 7c mistake.

And the mirror image is the strongest single signal in the entire archive: **a
market whose ask never dips wins 99.3% of the time.** It is not tradeable at
entry, being known only in hindsight, but it is not useless - it belongs to
LIFECYCLE management. Once a position is held, whether it has dipped is nearly
decisive about whether it will win. That is the next thing worth testing, and
it is a different question from both direction and entry timing:

  * direction - closed, the price wins (section 36)
  * entry timing - closed, waiting loses 7c (here)
  * **lifecycle/exit - open, and the 99.3%/54.1% split says there is real
    structure in it**

The oracle ceiling of +0.2484 says a quarter of a dollar per contract exists
for anyone who can tell a dip that recovers from a dip that does not. Nothing
measured so far can.

## 38. Meta-labelling: the first result that points the right way (2026-09-22)

The operator's reframing, and it is the correct one: the intelligence must not
compete with Kalshi's price on direction. It answers a narrower question -
*the base strategy says this qualifies; do historically similar QUALIFIED
setups show enough loss risk to skip it?* - and is graded in dollars:

    veto value = losses avoided - profits missed from false vetoes

**Why this is winnable where sections 36 and 37 were not.** At 80c a correct
veto saves the whole 80c stake while a false veto costs only the 20c forgone
profit, so four false vetoes are paid for by one correct one. A veto pays
whenever the vetoed subset wins LESS than its price - the model does not need
to be right, it needs to find a pocket where the favourite-longshot edge
reverses.

Measured on both populations, separately, as specified.

### Population 1 - all qualified signals (the training population)

1,700 of 6,428 corpus markets qualify under the deployed rule. Walk-forward,
1,415 out of sample:

| veto when P(loss) >= | vetoed | win% vetoed | avoided | missed | VETO VALUE | 95% CI |
|---|---:|---:|---:|---:|---:|---:|
| 20% | 742 | 74.4% | 142.59 | 131.84 | **+10.75** | -10.96 to +34.17 |
| 25% | 354 | 72.3% | 72.57 | 64.18 | **+8.39** | -7.52 to +24.83 |
| 30% | 77 | 70.1% | 17.04 | 12.82 | +4.22 | -3.45 to +12.48 |

Base strategy against base plus veto, same signals, same order:

| policy | trades | P&L | max drawdown | win% |
|---|---:|---:|---:|---:|
| base strategy alone | 1415 | **-12.18** | **-18.44** | 78.0% |
| + veto at P(loss)>=20% | 673 | **-1.43** | **-11.89** | 81.9% |
| + veto at P(loss)>=25% | 1061 | -3.79 | -12.47 | 79.8% |
| + veto at P(loss)>=30% | 1338 | -7.96 | -18.23 | 78.4% |

Four things point the same way, which is more than sections 33, 36 or 37 ever
managed:

  * the effect is MONOTONIC in the threshold - tighter vetoes help more
  * drawdown falls 35% (-18.44 to -11.89), not just P&L
  * it beats the random-veto control of the same size (+8.39 against +2.34)
  * it is positive in ALL FOUR sessions (asia +0.84, europe +2.95, late-us
    +4.16, us +0.44) rather than being carried by one cell

And two that do not:

  * **every interval spans zero.** +8.39 [-7.52, +24.83] is not a finding.
  * the decile table shows no monotonic edge structure - the negative-edge
    deciles are scattered (1, 2, 5, 8, 10), which is what noise looks like.

Note also what the base line says: the qualified population is **-12.18 out of
sample**, so the veto is reducing a loss rather than growing a profit. That is
still worth having; it is not the same claim.

### Population 2 - actual live executions (the executability check)

44 qualified live signals, 37 executed, 86.4% win rate at an average ask of
0.81 - an edge of +5.4 points over the price. Realised P&L on the executed
subset, read from the exchange: **+2.98**.

A perfect oracle veto could have saved at most **+4.80** across 6 losers;
vetoing everything would have cost **-1.78** net. So on the live sample the
qualified population is healthy and there is little for a veto to do. The two
populations disagree, and 44 signals cannot resolve that.

### What it would take

Per vetoed decision the effect is **+0.0237** with a standard deviation of
**0.4386**. For 80% power at 5%:

    n = (1.96 + 0.84)^2 x 0.4386^2 / 0.0237^2 = 2,685 vetoed decisions
    at the observed 25% veto rate, ~10,700 qualified signals

Against 1,415 corpus out-of-sample and 44 live. **Verdict: REMAIN SHADOW** -
but this is the first line of investigation that deserves to keep running
rather than to be closed. Sections 36 and 37 were refuted; this one is merely
underpowered, which is a different and better problem.

## 39. The loss recovery was never built (2026-09-22)

The operator: *"The recovery step never triggers after a loss. The $2 only
happened one time before and I don't see it again, it should trigger right
after a loss."*

Correct, and the reason is that **no loss-triggered recovery existed in the
code**. What was deployed is `high_confidence_contracts = 2`, which fires on
the distance/momentum band and has nothing to do with the previous outcome. The
three live losses of 2026-09-22 were each followed by a $1 trade:

    07:30  1 ct  -0.78  ->  07:45  1 ct   (should have been $2)
    08:30  1 ct  -0.88  ->  09:15  1 ct   (should have been $2)
    10:15  1 ct  -0.88  ->  10:30  1 ct   (should have been $2)

The $2 orders that did appear (05:45, 06:15, 07:15, 08:00) were band-triggered,
on windows with no preceding loss. Both readings of the conversation were
implemented as one, and the wrong one shipped.

**Now both trigger, independently.** The distance band keeps its $2, a loss
arms its own $2 whether or not the next setup lands in the band, and the two
never stack - the cap is `high_confidence_contracts` either way, so this cannot
become a martingale.

### Per loss, not a running ledger

"recover the lost two dollar AND RESET" reads as per-loss, and the measurement
says it has to be. Over 94 settled markets:

| rule | $2 on | recoveries completed | debt left |
|---|---:|---:|---:|
| cumulative ledger | 87/94 (93%) | 3 | $6.68 |
| **per loss, resets** | 54/94 (57%) | **11** | $0.71 |

A cumulative ledger never clears, because in this account 0.63-dollar average
wins do not keep up with 1.48-dollar average losses - it would have become
permanent flat $2 with $6.68 still outstanding. A new loss therefore REPLACES
the recovery target rather than adding to it, which also makes escalation
impossible by construction.

### Derived from the broker, not from a counter

`Store.outstanding_loss` replays settled `pnl` from the exchange mirror rather
than keeping a stored counter. A counter has to survive restarts, crashes and
manual trades, and each of those is a way for the live size to drift from what
the record says it should be. Replaying is idempotent and cannot disagree with
the money.

### The cost, recorded beside the decision

Flat $2 measured better than both sizing rules on the operator's 18 trades of
2026-09-21 (+2.96 against +1.96 deployed and +1.48 flat $1), and the $2
recovery measured +1.43 there against +1.48 for doing nothing, because the two
losses that day fell back to back and the recovery was holding double size when
the second one landed. The operator has asked for loss-triggered recovery; it
is implemented as asked, capped, and this is the number to watch.

---

## 40. Close-call exits, and the feed that cannot see the strike (2026-09-22)

The operator's proposal, after `KXBTC15M-26SEP221130-30` lost the full 77c: in
the last two minutes the entry buffer is irrelevant, so a position sitting on
the wrong side of the strike should be sold at the executable bid rather than
risk the whole stake. Section 2 closed the general stop-loss but never
conditioned any variant on TIME REMAINING, so this is a new rule and was
measured rather than assumed - `scripts/measure_close_call.py`, 4,279 qualified
trades from the 6,435-market corpus, live side convention, paired against hold,
net of both fees and the deployed 1c exit slippage.

### The deciding number

An exit is worth taking only where the crossed subset wins LESS often than its
own bid. It does not.

| last N min | crossed | mean bid | win rate | edge vs bid |
|---|---|---|---|---|
| 1 | 629 | 0.217 | 21.8% | +0.001 |
| 2 | 722 | 0.271 | 28.0% | +0.009 |
| 3 | 784 | 0.309 | 32.0% | +0.011 |
| 5 | 853 | 0.369 | 36.9% | +0.000 |

**At one minute out the bid is accurate to a tenth of a cent.** Kalshi prices a
late adverse cross essentially perfectly, so closing into it pays the spread a
second time and a second fee for nothing - section 2's argument, now confirmed
in the exact regime that was supposed to be its exception.

### Every variant of the policy, against holding

| window | min bid | fired | would have won | exit - hold | 95% CI |
|---|---|---|---|---|---|
| 1m | none | 389 | 35% | -0.0014 | [-0.0044, +0.0016] |
| 1m | 0.30 | 160 | 70% | -0.0005 | [-0.0027, +0.0017] |
| 2m | none | 620 | 32% | -0.0040 | [-0.0082, +0.0002] |
| 2m | 0.30 | 272 | 62% | -0.0022 | [-0.0055, +0.0012] |
| **3m** | none | 738 | 34% | **-0.0052** | **[-0.0102, -0.0002]** |

Nothing is positive and the widest window is significantly negative. The scale
matters more than the significance: hold-to-settlement earns **+0.0028/contract,
+$11.91 in total** across these 4,279 trades, and the 2-minute exit gives back
**-$17.23**. The policy costs more than the entire edge it is protecting.

The "would have won" column is the reason. A bid floor makes the rule fire less
often and more wrongly - at 0.30 it sells 272 positions of which **62% go on to
settle at $1.00**. A high bid near expiry does not mean rescue, it means the
market has not yet given up on the position, and those are precisely the ones
that recover.

### The discriminators the proposal named, each asked the same question

Cross depth, how long the cross had persisted, distance velocity and short-term
volatility, bucketed inside the last two minutes (n=722):

| discriminator | best bucket | n | mean bid | win rate | edge vs bid |
|---|---|---|---|---|---|
| cross depth | < 2 bps | 201 | 0.448 | 42.3% | -0.025 |
| cross depth | >= 20 bps | 41 | 0.016 | 0.0% | -0.016 |
| minutes offside | 2-3 | 82 | 0.270 | 23.2% | -0.039 |
| velocity | all buckets | - | - | - | positive |
| volatility | all buckets | - | - | - | positive |

None is usable. The shallow-cross bucket's 2.5c is inside the ~2.7c round trip
(1c slippage plus two fees at those prices). The deep-cross bucket is genuinely
dead - 0% of 41 - but its bid is 1.6c, so there is nothing left to recover; the
market has already taken it. "Offside 2-3 minutes" is -0.039 while 3-4 is
+0.019 and 4+ is +0.026, which is non-monotonic and therefore noise, in one
bucket out of fourteen examined.

### Why it cannot work here: the system cannot see the strike

The decisive fact is not about exit policy at all. **The settlement oracle is
not Binance spot, and the gap is far larger than the distances a close-call
rule would trade on.** Comparing each market's final-minute Binance close with
its `expiration_value`, over all 6,435 settled markets:

| | |
|---|---|
| median absolute basis | **6.00 bps** |
| p90 / p99 | 10.81 / 14.98 bps |
| mean signed basis | **+5.5 bps** (Binance reads high) |
| markets where spot and oracle disagree on the OUTCOME | **1,247 (19.4%)** |
| final minute within 5 bps of strike | 1,697 (26.4%) |
| ...of those, outcome flipped | **688 (40.5%)** |

Inside the close-call zone the feed the bot watches is wrong about which side
won **two times in five**. `binance.py` is the only price source in the
codebase; nothing ingests Kalshi's settlement index.

**Correction (section 41).** The table above compares a single Binance
minute-close against a 60-second average, which mixes the FEED difference with
the TIME AGGREGATION and can attribute neither. Measured apart, the gap is
**almost entirely feed**: aggregation contributes a median 0.72 bps of the 6.22
and removes none of the outcome disagreement. The verdict on the close-call
exit is unaffected - the 19.4% figure is still the right description of what
the bot can see - but the attribution in this section was not established by
this measurement. Section 41 does it properly.

The losing trade shows it directly. At 1:58 remaining the Kalshi app read
$86,281.91 - **$9.10 below** the $86,291.01 strike, with DOWN comfortably in
the money - while the bot's own archived observations at the same moment read
$86,291.23 and $86,298.19, i.e. **above** the strike. The two feeds disagreed on
the SIGN. A close-call exit driven by the Binance feed would have sold a
position its own broker's index still had winning. At 0:48 the app read
$86,292.77 (+$1.76, **0.2 bps**) against the bot's $86,316.67 - a $24 gap, 14x
the margin being adjudicated.

**A rule that acts on a 0.2 bps margin using a feed with a 6 bps median basis is
not measuring the thing it is deciding about.**

### What the entry actually did, contrary to the account of it

The "6.2x volatility buffer" did not collapse in the last two minutes. The
archived path shows it gone at **7:22 remaining** (normalized distance 0.09,
BTC $86,286 against a $86,291 strike) and the favoured side flipping to UP at
**6:21**. The position was underwater from -0.17 at 7:46 onward and never
recovered. There was no late reversal to catch; there was a trade that was
wrong five minutes after it was placed, which is what a 20% loss rate looks
like from the inside.

**Not deployed. Stop-loss stays off, in the last two minutes as everywhere
else.**

### What is worth building instead

The oracle ceiling is real and it is large: exiting only the late-crossed
positions that actually go on to lose is worth **+0.0169/contract, +$72.29** at
a 2-minute window - **six times the entire hold edge**. Section 37 found the
same shape in the dip. The structure exists; no feature measured here, or
there, separates it.

The one lever that has never been tried is the input, not the model. **Ingest
Kalshi's own settlement index and record it beside the Binance price at every
poll.** 40.5% of close calls being coin flips is not irreducible noise - it is
the error bar of the wrong instrument, and it is the only reason the deciding
number has nothing in it. Recording the basis costs nothing, changes no order,
and is a precondition for any future close-call study being worth running. Until
that data exists, a shadow HOLD/EXIT log would only be recording decisions made
on a feed that cannot see the strike.

---

## 41. The settlement reference: feed, not averaging (2026-09-22)

Section 40 blamed a 6 bps gap between Binance spot and the settlement oracle
for making close-call exits unmeasurable, but it compared **one Binance
minute-close against a 60-second average** and so folded two different things
into one number. The operator caught it. Separated, they are not close to equal
and the conclusion changes.

### What the contract actually settles on, from Kalshi rather than assumption

`GET /series/KXBTC15M` and `rules_primary` on every market:

> "If the simple average of the sixty seconds of CF Benchmarks' BRTI before
> 12:15 PM EDT is at least the simple average of the sixty seconds of CF
> Benchmarks' BRTI before 12:00 PM EDT, then the market resolves to Yes."

**Both ends are 60-second BRTI averages.** The strike is not a spot print
either - it is the same statistic taken at the window open - which nothing in
this system modelled. The identity that follows is exact:

    floor_strike[N] == expiration_value[N-1]

on **6,420 consecutive pairs, max difference $0.0000**, and
`expiration_value >= floor_strike` reproduces **6,435 of 6,435** settled
results. So Kalshi's own API publishes two official BRTI 60-second averages per
window, for free, and the app display never needs to be read.

### The decomposition

Each line holds one variable fixed. 1,416 settled markets, true per-second
Binance bars over the identical 60 seconds the contract settles on.

| | median abs | p90 | mean signed |
|---|---|---|---|
| **FEED** (60s Binance vs 60s BRTI) | **6.23 bps** | 9.90 | +5.80 |
| **AGGREGATION** (Binance last vs Binance 60s) | **0.72 bps** | 3.29 | +0.13 |
| TOTAL (what section 40 reported) | 6.22 bps | 10.61 | +5.94 |

| outcome disagreement with official | | |
|---|---|---|
| Binance last tick (section 40) | 270 / 1,416 | 19.07% |
| Binance 60-second mean | 273 / 1,416 | 19.28% |

**Averaging is not the problem.** Switching from a last tick to a 60-second
mean - the obvious fix, and the one the proposal implied - changes the
disagreement by three markets in the wrong direction. It is noise. The entire
gap is the feed: Binance BTCUSDT against a multi-venue USD index.

### The basis is systematic, and that is the useful part

Mean +5.80 bps, and it drifts by period while staying tight within one:

| period | n | median | sd |
|---|---|---|---|
| 2026-07 mid | 511 | +6.40 | 1.23 |
| 2026-08 mid | 157 | +9.46 | 2.47 |
| 2026-08 late | 174 | +1.04 | 1.51 |
| 2026-09 early | 158 | +1.58 | 2.23 |
| 2026-09 mid | 85 | +3.09 | 1.99 |

A fixed constant would therefore be wrong most of the time, but a **trailing
median of the previous 20 settled windows - fitted on earlier markets only,
never on the one being scored** - tracks it:

| | raw | calibrated |
|---|---|---|
| residual median error | 6.23 bps | **0.76 bps** |
| outcome disagreement | 19.41% | **4.01%** |

**Four fifths of the disagreement is a correctable basis, not irreducible
noise.** The remaining 4% is what an actual BRTI subscription would buy.

### The recorder

`reference_shadow.py`, `reference.py`, `reference_store.py`, wired into the
service behind the trading path on the same contract as the hourly shadow.
Shadow only: no entry, exit or sizing path reads any of it, and there is
deliberately no flag that changes that.

* **BRTI is the only reference.** CF Benchmarks gates index values behind an
  entitlement, so without `CFB_API_KEY` every poll writes a `missing` row
  naming the reason and `basis_bps` stays NULL. Binance is recorded in its own
  column as a comparison and is **never** promoted into the reference column;
  no other exchange is ever substituted. A basis measured against a stand-in is
  not a basis.
* **Gaps are records.** Missing, stale and errored polls are written with the
  reason, collapsed one row per run, so coverage is auditable rather than
  assumed - a recorder that only writes when the feed works cannot be told from
  one that was switched off.
* **Staleness is measured, not trusted.** Event timestamp, receipt timestamp
  and age are all stored; past `reference_stale_ms` the row is `stale` and
  carries no price, because a stale print averaged in as current is exactly how
  a 60-second mean goes quietly wrong.
* **Named-column inserts and `PRAGMA user_version` migrations**, because a
  positional insert is what put 93 shadow rows into permanent quarantine
  (section 38).
* 14 tests in `tests/test_reference_recorder.py` pin the harmlessness contract:
  a feed that raises on every call must leave `poll` and `reconcile` silent.

### Where this leaves the HOLD/EXIT model

**Not started, as specified.** `computed_brti_error_bps` is the gate and it is
currently unpopulated: 0 markets have our own BRTI ticks, because the feed is
not entitled. Until the recorder reproduces official settlements from its own
observations, any shadow decision it logged would be a decision made on the
wrong number - the precise mistake section 40 exists to record.

**The single highest-value action is a CF Benchmarks entitlement.** Everything
else is built and running.

    python scripts/reconcile_settlement.py --limit 400   # decompose + calibrate
    python scripts/reconcile_settlement.py --report-only

---

## 42. Kalshi serves BRTI itself. Two prior conclusions were wrong (2026-09-22)

Sections 40 and 41 were built on a premise that is false: that the settlement
reference is unreachable without a CF Benchmarks subscription. **Kalshi
publishes BRTI through its own API, to our existing production credentials.**
The operator said so; this section is the verification, and the correction.

**What was wrong, and why.** Section 41 concluded "no BRTI passthrough" after
probing fourteen invented routes (`/index/BRTI`, `/indices`, ...), authenticated
and not, and getting 404 on all of them. The routes were guesses. The published
OpenAPI and AsyncAPI specifications name the real ones. **Guessing at an API
surface and reporting the absence as a finding was the error** - the specs were
one fetch away and are now the authority for every contract here.

### The endpoints, verified against production

| purpose | call |
|---|---|
| per-second BRTI, charting | `GET /live_data/events/{event_ticker}` |
| raw RTI prints | `GET /cfbenchmarks/values?id=BRTI` |
| historical RTI, **200 ms** | `GET /cfbenchmarks/history/values?id=BRTI&timespan=HOUR&timestamp=...` |
| index catalogue | `GET /cfbenchmarks/info` (BRTI `decimals: 2`) |
| live stream | WS channel `cfbenchmarks_value` |

Signing gotcha, from the docs and confirmed the hard way: **sign the path
without the query string**. `_headers` already prefixes `/trade-api/v2`, so
passing it again produces `INCORRECT_API_KEY_SIGNATURE`.

The passthrough costs **50 read tokens** against 10 for an ordinary request,
so it is a reconciliation and backfill tool, not a per-poll one. The
`cfbenchmarks_value` channel is the live path: authenticated, roughly one tick
per second, and it carries **the raw upstream frame plus trailing 60-second and
quarter-hour final-minute averages**. Kalshi computes the settlement statistic
and streams it. A 5Hz sibling channel covers BTC.

### The settlement value is readable exactly, live, as it forms

`live_data.details.timeseries` is one point per second, and each `v` is **already
a trailing 60-second mean** - which is why averaging sixty of them fails badly
(±50 dollars, 0/25): that double-smooths. The value AT the close instant is the
settlement:

| function of the series | reproduces `expiration_value` |
|---|---|
| mean of the 60 points in the final minute | **0 / 25** |
| **the single value at close** | **12 / 12 exact** |
| value at close - 1s | 12 / 12 (to $0.003) |
| the 1M candlestick close of the final minute | **12 / 12 exact** |

So the quarter-hour final-minute average is observable **second by second as it
accumulates**, not only after settlement. At T-30s the system can read what the
contract would settle at if the window ended now - the exact quantity section 40
said was unknowable.

### Our own recomputation: a tripwire, not the source

Recomputing the average from raw 200 ms history lands **$0.2-$1.1 from official
(0.02-0.13 bps)** but never to the cent, under every window alignment and
per-second sampling rule tried. Against Binance's 6.23 bps that is a hundredfold
improvement, and it is still not the official number.

**Therefore: consume Kalshi's computed average as the source of truth and use
the independent recomputation only as a disagreement alarm.** Recomputing what
the exchange already publishes, and then trading on our version, would reinvent
exactly the instrument mismatch this whole line of work exists to remove.

### What this re-opens

Section 40 closed close-call exits on the measurement that the crossed subset
wins at its own bid. That measurement used **Binance** distance, and section 41
showed Binance disagrees with the official outcome 19% of the time and 40% inside
5 bps of the strike. The discriminators - cross depth, cross duration, velocity -
were therefore all computed on the wrong instrument.

**The close-call question is re-opened, on BRTI, and only on BRTI.** The oracle
ceiling of +$72 against a +$11.91 hold edge (section 40) is the prize. Nothing is
re-deployed on the strength of this section; the entry rule, the exits and the
sizing are unchanged until a Kalshi-native measurement says otherwise.

### What stands from sections 40 and 41

- The 19.4% outcome disagreement between Binance and official settlement. Still
  right, still the reason for all of this.
- The FEED/AGGREGATION decomposition: 6.23 bps versus 0.72 bps. The averaging
  was never the problem.
- The walk-forward basis calibration, 19.4% -> 4.0%. Now obsolete as a
  **decision** input, since the official number is directly readable, but kept
  as the measurement that proved Binance's error was structured rather than
  random.
- `floor_strike[N] == expiration_value[N-1]`, exact on 6,420 pairs.

---

## 43. The BRTI pipeline, and the thresholds that cannot come with it (2026-09-22)

`brti.py` reads the settlement reference from Kalshi and computes the gate
inputs from it. Shadow only; no entry, exit or sizing path reads any of it.

**Source choice.** `/live_data/events/{event_ticker}`, not the
`cfbenchmarks_value` WebSocket, for a first deployment. Every response carries
the entire window at one point per second, so a dropped poll costs freshness
and never leaves a hole; there is no sequence state to lose and no reconnect to
get wrong. It is also ordinary cost, against 50 read tokens for a passthrough
call. The WebSocket is the upgrade when sub-second latency matters and it fits
behind the same interface. The passthrough keeps its place for backfill and
for the raw prints.

**Verified on the live feed**, not assumed: each published point is a trailing
60-second mean, and the mean of the raw 200 ms prints over the matching minute
reproduces it to a median **$0.247** on an $86,000 index - 0.03 bps. The two
descriptions of the series agree.

### The thresholds do not transfer, and the size of the gap is the point

A 60-second mean is a low-pass filter, so volatility measured on it is not the
quantity `min_normalized_distance` was calibrated against. Measured live:

| | Binance raw | Kalshi BRTI 60s mean |
|---|---|---|
| step-to-step volatility | 1.0x | **0.67x** |
| `normalized_distance` on the same market | 2-4 typical | **15 - 22** |

The deployed gate is `min_normalized_distance = 1.5` and the confidence band is
2.0-4.0x. **On BRTI those numbers would pass essentially every market and size
up on most of them.** Copying the thresholds across would not be a migration,
it would be switching the distance gate off and the confidence sizing on, while
the config still read as though nothing had changed.

So the BRTI fields are named apart from the Binance ones - `brti_momentum_bps`,
`brti_volatility_bps`, `brti_normalized_distance` - and `tests/test_brti_native.py`
asserts the names stay distinct, so a threshold measured for one cannot be
applied to the other by autocomplete.

### What is recorded now

`brti_features`, one row per poll: the official value, signed distance to the
official strike, BRTI momentum and volatility, the implied side, the settlement
projection, sample count and staleness - with the Binance view of the same
instant beside it as **archive**. `sides_agree` counts the live disagreement
rate that FINDINGS 41 measured at 19.4% on history.

`tests/test_brti_native.py` pins the separation structurally: the module's AST
is checked for Binance imports and its code for Binance field names, so the rule
survives future edits rather than depending on the author remembering it.

**Not deployed, and deliberately not next.** The next step is not a strategy
file, it is a measurement: recompute the gates on BRTI history and find the
thresholds that mean on this instrument what 1.5 and 2.0-4.0 meant on the old
one. A `kalshi_brti` strategy written before that measurement would be the
deployed rule with its gates silently disabled.

---

## 44. Sizing: the band switched off, and recovery rebuilt on realised money (2026-09-22)

Two sizing changes, both the operator's decision, both made after a single
trade doubled its exposure and lost.

### The trade

`KXBTC15M-26SEP221330-30`, 2 contracts at 81c, reported at the fill as
*"Size 2 · 3.0x vol is inside the measured 2-4x edge band"*. It settled
against us: **-$1.6416**, where one contract would have been about -$0.82.

### 1. Confidence sizing is OFF

The operator: *"The intelligence doubled exposure because of confidence. That
contradicts the earlier requirement that intelligence must not change sizing."*

`confidence_sizing_enabled: bool = False` now gates `confidence_size()`, which
returns `(base, "")` when off - an empty reason, so no size line claims a band
that nothing acted on. `high_confidence_contracts` is NOT changed: loss
recovery reads the same number, and the two mechanisms are meant to stay
independent.

The 3,841-entry measurement behind the band (section 26, +0.0359/ct in
2.0-4.0x against +0.0149 overall) is left in the code, not deleted, because it
is still what was measured. It is also now in question on its own terms: the
`normalized_distance` it was fitted on is Binance-derived, and sections 41 and
43 measured that quantity disagreeing with Kalshi's official BRTI reference on
about 20% of markets. Re-enabling the band needs a number, not a preference.

### 2. Recovery: what was deployed, and why it escalated

What shipped in section 39 was *per loss, replace the target, size up until it
is repaid*, derived by replaying `settlements`. Two consequences, both visible
in the live record of 2026-09-22:

| | window | contracts | realised | debt after |
|---|---|---:|---:|---:|
| loss | 1130-30 | 1 | -0.7824 | 0.78 |
| recovery | 1200-00 | 2 | +0.2441 | 0.54 |
| **still upsized** | 1245-45 | 2 | +0.5515 | 0.00 |

A recovery that wins but does not repay in full keeps sizing up - and a
recovery that *loses* replaces the debt with its own, larger loss and keeps
sizing up against that. The cap on the contract count never stopped this,
because the escalation was in the number of upsized trades, not their size.
There was also no check that an upsized trade could win enough to matter: 2
contracts at 90c risk $1.80 to win 19c.

### 3. Recovery now: a deficit, and a trade that can pay for it

The operator's rule, implemented as stated:

1. Activates on a **realised net loss**, after fees.
2. Tracks the unrecovered **deficit**; a further loss INCREASES it and never
   restarts or erases it.
3. Recovery ends when realised profit has covered the deficit - `$0.00 or
   better` - and not on a win count, paper profit, open mark or gross profit.
4. Recovery **never creates a trade**. Every strategy gate has already passed
   before sizing is reached; recovery only changes the size of a trade that
   was happening anyway.
5. The deficit is divided across the remaining planned steps
   (`recovery_steps: int = 4`), and the upsize applies **only if this trade's
   maximum net profit covers that share**. Otherwise the trade goes out at
   base size and recovery stays active.

        max_net_profit(count, ask) = count * (1 - ask) - kalshi_fee_charged(ask, count)

        deficit 1.64 over 4 steps -> 0.4104 a trade
        2 @ 0.90 -> 0.1874 net  ->  base size
        2 @ 0.75 -> 0.4737 net  ->  recovery size

The feed is **`daily_ledger`**, not `settlements` and never a local rebuild: it
is the append-only realised record, it counts an early cash-out exactly once,
and only the exchange may revise a figure it holds. `recovery_applied` on that
table stores the amount already folded into the deficit, so a row the exchange
revises moves the deficit by the **delta** - a cash-out banked at +0.5515 and
settled at +0.5480 costs one cent, not another 55. The deficit itself is one
persisted row (`recovery_deficit`), so a restart mid-recovery resumes.

Three transitions, one line of arithmetic: `deficit = max(0, deficit - realised)`.

### Judgement calls, stated so they can be overruled in one line

- **No midnight reset.** The deficit is about money that is still missing, not
  a calendar. `auto_daily_loss_limit` remains the day's own stop. Clearing the
  `recovery_deficit` row daily is the change if the operator wants one.
- **Steps decrement only on an upsized trade**, and only on a fill. A trade
  held at base because it could not cover its share has changed no plan, and
  an order that bought nothing spent nothing. This keeps the requirement
  roughly flat as the deficit falls: 1.64/4 = 0.41, then 1.17/3 = 0.39.
- **A new loss resets the steps to the full plan.** Otherwise the divisor
  shrinks while the deficit grows and the required share explodes, switching
  the upsize off exactly where it is wanted on.
- **A deficit below half a cent is $0.00.** The account trades whole cents.

### The measurement this decision overrides

Section 39 chose per-loss replacement on a measurement: over 94 settled
markets a cumulative deficit would have sat at recovery size for 93% of
windows with $6.68 still outstanding and 3 recoveries completed, against 57%
and 11 for per-loss. The operator has decided for the cumulative deficit with
that measurement in view, and the eligibility gate is new since it was taken.
The number to watch is how long the deficit stays open.

### Pinned by tests

`tests/test_intelligence.py`: the band sizes one contract while the flag is
off and recovery is unaffected by it; a loss opens a deficit of exactly its
realised net amount; a partial recovery keeps recovery on; the profit that
clears it turns recovery off and the next trade is base; a second loss
increases the deficit rather than restarting it; an early cash-out contributes
once; an exchange revision moves the deficit by the delta only; the deficit
and the step count survive a `Store` reopen; the operator's worked example at
both prices; an ineligible trade goes out at base with recovery still active;
a base-size win still pays the deficit down; steps decrement only on upsized
trades and a loss resets them. Two structural tests hold the shape: recovery
is unreachable before the entry gates, and the step is consumed after the
order call and only on a fill.

### On deployment

Not restarted here. On the first start after this change the deficit folds the
existing `daily_ledger` history once: at 13:45 that is **$1.2449 outstanding**
over 4 steps, $0.3112 a trade, which 2 contracts can cover at 81c and cannot
at 87c. The day's realised total is +$3.45 - the deficit is path-dependent and
floors at zero, so it measures the money missing since the last time the
account was whole, not the day.

---

## 45. The conditional recovery add-on: deployed configuration (2026-09-22)

Shipped after the order-safety tests passed, on the operator's $30 live-test
authorisation. **Profitability is what this test decides; it is not claimed
here.** The corpus measurement that motivated it (section 43, the 2c
conditioned add at +0.0621 [+0.0087, +0.1159]) cannot settle it, because a
minute candle records that the ask REACHED a price, not that a queued order
traded.

### What is running

| | |
|---|---|
| base entry | **1 contract, always** - recovery never enlarges it |
| recovery upfront upsize | **off** (`recovery_upfront_upsize_enabled`) |
| confidence-band sizing | **off** (`confidence_sizing_enabled`) |
| the add | 1 contract, resting 2c below the ACTUAL fill |
| add deadline | 120s before close, with its own `expiration_time` |
| distance floor | 10.0 BRTI normalized distance |
| authorised cap | **$30 cumulative PURCHASE SPEND** |

### The three limits, deliberately not merged

The $30 is a **purchase-spend** ceiling: every dollar ever paid for add
contracts, plus whatever rests unfilled. It is **not a loss budget** - a run of
PROFITABLE adds exhausts it just as fast, because the money was spent either
way and returned as settlement rather than as headroom. `add_budget_state`
reports purchase spend, current resting exposure and realised add P&L
separately, so a report cannot quietly substitute one for another.

### Three defects found by deploying it

**The epoch.** Widening the settlement lookback to catch windows that settle
after midnight pulled three days of finished markets into the ledger
unapplied. The fold replayed them and drove the deficit **$0.85 -> $10.90**.
Nothing looked broken: recovery still reported ACTIVE. But the requirement is
deficit/steps, so it became **$2.73 a trade**, which no two-contract position
can reach - the add-on would have skipped every setup for a reason that read
like arithmetic rather than a fault. Backfilled history is now marked applied
on arrival, keyed on the MARKET's own time, so a trade made after the epoch
still counts however late the broker delivers it.

**Crossing measured from the wrong instant.** The first live evaluation vetoed
an add on `KXBTC15M-26SEP221500-00` for "BRTI crossed the strike since entry".
The add was evaluated at 18:49:40; the base fill happened at **18:49:42**. The
check looked back to the 18:45 window open, so 4m42s of history from before
the position existed was allowed to veto it. Entry time now comes from the
broker's own fill, and an entry time that cannot be established is UNKNOWN -
which refuses the add rather than silently reading as "crossed".

**Order-sensitive rebuilding.** The deficit floors at zero, so profit stops
reducing it and the replay order is part of the answer. It was ordered by
`window_ms`, which is wrong: an early cash-out realises while its own window
is still running and can realise BEFORE a market that opened earlier, so a
profit could be applied to a debt that did not exist yet. It now replays in
realisation order with `ticker` as a stable tie-breaker, so a duplicate sync
and a restart produce the same number.

### What the first deployment actually proved

One add, SKIPPED, on the crossing condition - which is the refusal path and
nothing more. Placement, fill, partial fill, the cancel-versus-fill race,
restart reconciliation, separate add P&L and recovery reaching zero are
covered by 16 tests in `tests/test_recovery_lifecycle.py` and have **not** yet
been demonstrated against the exchange. That distinction is the whole point of
the live test and should not be blurred in a status report.

### Still open

The daily account-growth sizing controller is NOT part of this and is not
demonstrated: start-of-day balance reconciliation, one shared sizing
authority, and a fresh exposure check before each order. Keeping it separate
from recovery is deliberate - two independent things that both change size are
exactly how a cap gets exceeded by the sum of two rules each of which looked
bounded.

## 46. Forward evaluation on BRTI, and two defects that faked it (2026-09-22)

The adaptive layer was rebuilt on the deployed BRTI features and given a
forward-evaluation path, so a candidate adjustment accumulates evidence on
data it was never fitted to **without** controlling any order. Promotion and
forward-testing are separate bars:

| | question | needs |
|---|---|---|
| candidate | worth watching forward? | n >= 60 |
| promotion | may it change a live order? | n >= 120, validated, CI clear of zero |

### The BRTI corpus

The stride-3 backfill gave 2,143 markets and no cell reached n=60, so the
earlier "zero candidates" result was a **dataset-size artefact, not a finding**.
The full stride-1 backfill completed: **6,435 markets, 38,610 decision points,
0 fetch failures**, yielding 6,428 usable rows - the same count as the Binance
corpus, over the same 68 days.

An earlier FIRST-MINUTE reading of the training split gave +0.0128/ct over
679 taken and -0.0522/ct over 2,856 refused, with one `admit` candidate in
`us · low · bd5-10 · px<70`. **Both are superseded** by the policy figures
below - that reading judges each market once at 660s, which is not what the
bot does. They are recorded rather than deleted because they were reported,
and a corrected number needs the thing it corrects next to it.

Under either reading the gates are right on average, and more clearly so than
Binance measured them.

**One candidate frozen, zero promoted**, under both readings - a different
candidate each time, which is itself evidence that the scope mattered. The
policy-corpus candidate is described below.

### Two defects that would have faked the result

Both are the same species - *code that runs clean while measuring nothing* -
and neither would have shown up as an error.

**1. Grading was a tautology.** The settlement loop passed
`result == ("yes" if winning_side == "UP" else "no")` as `won`. `winning_side`
is derived from `result` one line above, so the expression is true by
construction. Every graded row scored a **win**: 21 of 21 to date. A veto
would always look like it blocked a winner, an admission like it caught one.
It happened to write nothing false yet only because the single settled window
was on the winning side. Both `grade_candidates` and `grade_intelligence` now
take the **winning side** and score each row on the side it was recorded on,
the way `settle_shadow` already did.

**2. Live and replay named different cells.** Candidates were frozen on BRTI
bands (`bd5-10`) while the live path built its key from Binance distance bands
(`dist1.5-3`) and Binance volatility thresholds (5/12 bps against BRTI's
0.5/1.5). The key still formats. It simply names a pocket no artefact
contains, so every candidate would have matched nothing and the table would
have stayed empty **forever** while the logs reported the layer integrated.

This is section 39's lesson recurring in a new guise - the first live decision
keyed `"? · ? · ..."` because the live snapshot had no `session`. The fix then
was to derive it the same way the corpus does; the fix now is stronger:
`brti_context_of` lives in `adaptive.py` and **both** the training script and
the live path call it. Two functions that agree by inspection is not the same
as one function.

The live key needs BRTI numbers, and the reference recorder polls *behind* the
trading path by design (FINDINGS 22: ~2,000ms of pre-order work cost three
fills). So the gate reads the **last** poll's features and `brti_context_row`
checks what that costs: features absent, stale, or belonging to another window
(`target` != this strike) all yield **no context and no row**. There is no
fallback to the Binance-scale numbers - a missing row is visible in the log,
a mislabelled one is not.

### What the replay actually models - and what it does not

**Correction, and it matters more than the one below it.** I called the
chronological replay "the deployed policy". It is not. It is a **proposed
BRTI-calibrated entry rule**, replayed in the order the bot sees minutes.
Qualification in this corpus is **not a confirmed trade and not a fill**.

| | deployed | replay |
|---|---|---|
| contract price band 70-93c | yes | **yes** |
| normalized distance | Binance, `>= 1.5` | **BRTI, `>= 10.0`** |
| momentum alignment | **not required** | **required** |
| model confidence `>= 0.50` | yes | **not modelled** |
| max spread 2.0 bps | yes | **not modelled** |
| **band-hold `entry_band_settle_s` = 60s** | yes | **not modelled** |
| one open position at a time | yes | **not modelled** |
| retries (`auto_retry_limit` 3, drift 0) | yes | **not modelled** |
| daily loss floor / trades per day | yes | **not modelled** |
| an actual fill | required | **assumed at the recorded ask** |

The band-hold is the one that bites hardest, and the config already says so:
entering on the FIRST qualifying minute measures **+0.0080/ct, CI [-0.0019,
+0.0180] - which does not clear zero**, while requiring time in the band
gives +0.0149 [+0.0032, +0.0260]. The replay does the first of those.

The live log from 2026-09-22 22:00Z shows it refusing five qualifying minutes
in one window on exactly that rule:

    21:50:34 auto[...DOWN@0.72 568s]: eligible
    21:50:34 auto: declined - price has only held the band 0s, waiting for 60s
    21:52:43 auto[...DOWN@0.80 438s]: eligible
    21:52:43 auto: declined - price has only held the band 0s, waiting for 60s
    21:52:56 ... held the band 14s, waiting for 60s
    21:53:10 ... held the band 27s, waiting for 60s
    21:53:52 ... held the band 0s, waiting for 60s

Five qualifying minutes, no trade. Earlier the same evening: `declined - 1
position(s) already open`. So the 3,495 "qualified" markets are an **upper
bound on opportunities**, materially larger than the set the bot would have
traded, and every per-contract figure computed from them describes a rule
that could be deployed, not the one that is.

That does not invalidate the comparison below - both legs are computed the
same way, so the *relative* first-minute-versus-chronological point stands -
but the absolute numbers are not the shipped strategy's P&L and must not be
quoted as it.

### The scope error: first minute is not chronological replay

**The figures above are first-minute analysis, and are labelled as such from
here on.** They judge each market once, at 660s remaining. The scanning rule
does not: it walks every poll from 660s to 360s, takes the **first** minute
whose gates pass, and stops. A market refused at 11 minutes and qualified at
8 is one the rule **selects** - and first-minute analysis files it under
"refused".

It mislabels **2,213 markets** that way, and the correction moves both legs:

| | rule took | rule refused | qualified |
|---|---:|---:|---:|
| first-minute only (660s) | +0.0128/ct over 679 | −0.0522/ct over 2,856 | 1,282 |
| **deployed policy (660→360s)** | **+0.0173/ct over 1,841** | **−0.1204/ct over 1,694** | **3,495** |

The policy reading is better on *both* sides, and it is better because the
first-minute refused leg was diluted with 2,213 trades the bot actually
takes. What the gates genuinely turn down is far worse than −0.0522: it is
**−0.1204/ct**. The gates are doing more work than the earlier number
credited them with.

Entry minute of the 3,495 qualifying markets: 660s 1,282 · 600s 545 ·
540s 564 · 480s 464 · 420s 344 · 360s 296. Only 37% are taken at first look,
which is the size of the error.

The other wrong summary is equally available and was never used: **one row
per poll**. That lets a single market contribute six correlated copies that
all share an outcome, inflating every sample count sixfold and every
confidence interval with it. `choose_minute` is the one place this is
decided, and `tests/test_policy_dataset.py` pins both failure modes.

### The corpus, reconciled

The baseline is quoted on the TRAINING split, which is why 1,841 + 1,694 =
3,535 and not 6,428. The split is chronological (55/25/20), never shuffled:
markets in one session move together, so a random split leaks the afternoon
into the morning.

| split | markets | qualified | rejected |
|---|---:|---:|---:|
| train | 3,535 | 1,841 | 1,694 |
| validate | 1,607 | 897 | 710 |
| holdout *(untouched)* | 1,286 | 757 | 529 |
| **total** | **6,428** | **3,495** | **2,933** |

Exclusions: 6,435 markets have BRTI decision points; **7** are dropped for
having no Kalshi quote at the matching minute, giving 6,428 usable. One row
per market, so a market cannot enter twice with six correlated copies of
itself.

### The candidate the policy corpus produced

Re-fitting on the policy corpus replaced the candidate entirely, which is its
own evidence that the scope error mattered:

    c01  VETO  asia · mid · bd10-15 · px85-94  (accept leg)
         train n=143 over 34 days, -0.0044/ct, CI [-0.0668, +0.0326]
         validate n=38, mean -0.0652 (agrees in sign)

It clears the n≥120 promotion threshold and validation agrees in direction -
but validation has n=38 against a required 40, and the training interval
crosses zero. **It does not promote.** Two markets short is still short, and
moving the threshold to fit the candidate in front of it is the one thing
that would make the bar meaningless.

**It is NOT the rejected-winner hypothesis.** I claimed it confirmed the
operator's earlier suspicion about demoting high-confidence signals. It does
not. That hypothesis was about *rejected winners* and about the model's own
confidence; this candidate is about a different group entirely - contracts
the rule **accepts**, selected by their **price** (85-94c), in one session.
Two different claims that happen to share the word "high". The rejected-
winner question remains open and this says nothing about it.

It is a clean illustration of why this file reports edge and not win rate.
The cell wins **126 of 143 on the training split - 88.1%** - and is still the
candidate the model wants to veto, because at 85-94c a contract needs about
90% before fees to break even. A long run of wins there is what losing money
slowly looks like.

**Holdout disclosure.** I first quoted this as "184 of 215 (85.6%)". That
figure spans train + validate + **holdout**:

| split | n | won | |
|---|---:|---:|---|
| train | 143 | 126 | 88.1% |
| validate | 38 | 31 | 81.6% |
| holdout | 34 | 27 | **79.4%** |

Candidate *selection* never read the holdout - the code fits on `train` and
tests `promotes` against `validate` only, and that is verifiable in
`train_brti_candidates.py`. But **I read it, and reported it**, so the
holdout is no longer clean *for this candidate*: any future argument I make
about c01 that leans on those 34 markets is circular. The train figure is the
one to quote, and the promotion evidence has to come from forward data the
model has never seen. Which is what forward evaluation is for.

The cell fires **3.16 times a day** across the corpus, so forward n=60 is ~19
days out and n=120 ~38 - faster than the first-minute candidate's 1.35/day,
but still weeks, not sessions.

### Feature parity, measured rather than asserted

Context parity means both sides call `brti_context_of`. That guarantees the
band *labels* come from one function; it says nothing about the numbers fed
in. A different lookback, cadence or smoothing on the live side would still
produce labels from the agreed function, and every one could be wrong.

Comparing distributions cannot settle it - live covers ~2 days against the
corpus's 68, so any difference in medians is confounded with regime. (For the
record it looks fine: live median volatility 1.09 bps against 0.77 historical,
which is a two-day sample sitting inside a 68-day spread.)

So `scripts/verify_feature_parity.py` recomputes instead. For each row the
LIVE path stored, it fetches the series the way `backfill_brti.py` does,
truncates to the same instant, calls `features_from_series` with the same
arguments, and compares against what live recorded.

**250 of 250 identical**, worst disagreement 1.3e-10 - floating-point
reassociation, nothing more:

| | max abs difference |
|---|---:|
| `brti_volatility_bps` | 1.7e-12 |
| `brti_momentum_bps` | 2.2e-12 |
| `brti_normalized_distance` | 2.8e-11 |
| `signed_distance_bps` | 1.6e-11 |
| `brti_value` | 1.3e-10 |

Both paths call `KalshiBRTI.series()` on the same endpoint, sort, and call
`features_from_series` with the default 300s momentum and 300s volatility
windows. The decision seconds match the live entry window exactly
(660/600/540/480/420/360 against `entry_from_seconds`=660,
`entry_to_seconds`=360).

One difference exists and is **immaterial, which is not the same as absent**:
live holds a rolling 3,600-sample hour, while the backfill's truncation to
`t <= cutoff` yields 2,941 samples at 660s remaining growing to 3,241 at
360s. Both features read only the trailing 300 seconds at 1 sample/second, so
the surplus never enters the arithmetic - and the 250/250 recomputation is
what establishes that, rather than the reasoning.

### A rounding note on the lifetime total

The total is summed from **unrounded** rows and rounded once at the end.
Adding up *displayed* components instead moves the answer by a cent, and the
cent is rounding, not a missing trade:

| as of | gross wins | gross losses | unrounded | shown | from rounded parts |
|---|---:|---:|---:|---:|---:|
| earlier this session (~112 mkts) | 43.2257 | −42.5142 | 0.7115 | **$0.71** | 43.23 − 42.51 = 0.72 |
| now (127 markets) | 49.8343 | −49.5277 | 0.3066 | **$0.31** | 49.83 − 49.53 = 0.30 |

Both rows are correct for their instant. **$0.71 was right when it was
computed and is not the current lifetime** - 15 further markets have settled
since, and the running total is now **$0.31**. Quoting the older figure as
today's would be the more damaging error of the two, so both are dated here.

Note the discrepancy flips sign between the rows: rounding components first
can round either way. The wrong fix is to round them so the subtraction
"works" - that would make the shown total disagree with the broker, which is
the one number this system is not allowed to invent (section 37).
`lifetime_record` carries the same note so nobody reconciles it backwards
later.

## 47. Kalshi-only, and the five Binance dependencies hiding in it (2026-09-22)

Binance is out of every active signal, intelligence, training and evaluation
path. Kalshi supplies quotes, books, executions, settlements and BRTI. The
Binance-trained policy is retired, historical records keep their source
labels, and there is no fallback: a missing or stale input is recorded and
produces no signal.

Deployed revision **a51f1f2**, running from 23:45:15. 743 tests pass.

### The mislabel that was already live

`runtime/intelligence_policy.json` declared `feature_version: brti-1` over
seven arms every one of which was keyed on **Binance** bands (`dist3+`, never
`bd10-15`). A Binance-trained policy wearing a BRTI label - exactly what the
version guard existed to stop, and exactly what it could not see, because it
compared the declared field to itself. It was inert only because both action
flags happened to be off.

Provenance is now read from the **arm keys**, not the metadata:
`keyed_feature_family` says what a policy actually is, `mislabelled` compares
that to what it claims, and `binance` is a retired family that cannot act
under any label. The artefact is relabelled `v1-retired` / `binance-1` and
refuses with *"policy is keyed on retired features binance-1"*.

### Five live dependencies, found five different ways

Removing the client was the easy part. What it exposed:

| # | where | found by | would have |
|---|---|---|---|
| 1 | `hourly.poll(now_ms, market)` | crash at 23:00 | killed the service on startup |
| 2 | `levels.maybe_refresh(market, …)` | 55s dry run | killed the service on the first poll |
| 3 | `await market.close()` | code read | raised on shutdown |
| 4 | `decision_record` → Binance `check_detail` | dry-run log | lost EVERY decision record silently |
| 5 | `ReferenceShadow._binance.latest()` | **netstat** | kept an open Binance connection |
| 5b | `._binance.seconds()` | live log line | broke second-bar decomposition quietly |

Number 5 is the one worth remembering. The signal path had been migrated, the
code read clean, 738 tests passed - and `netstat -ano` against the running
process showed an ESTABLISHED connection to `data-api.binance.vision`. The
recorder's Binance column is archive and nothing reads it to decide anything,
but **an archive column that costs a live request every ten seconds is an
active dependency however it is labelled.** I would have reported "no active
Binance dependency" and been wrong.

Verified after deploy: the service holds connections to
`external-api.kalshi.com` only.

### Thresholds do not survive a change of instrument

Two gates were calibrated on Binance and would have transferred silently:

* **distance.** `min_normalized_distance = 1.5` measures Binance RAW
  volatility; BRTI reads 10-20 on the identical market. Reusing it passes the
  gate on everything while still drawing a tick beside it. The active rule is
  `KalshiBRTIRule` with the measured 10x floor (FINDINGS 43).
* **spread.** `max_spread_bps = 2.0` gates Binance SPOT spread, whose p99
  over 10,094 archived observations is **0.001 bps** - it had never rejected
  anything. A 2c Kalshi spread on a 79c mid is **253 bps**, so carrying the
  number across would have discarded *every* signal. Replaced by a gate in
  cents at ~p99 of the measured contract distribution (median 0.4c, p95 10c,
  p99 19c), plus an explicit refusal for crossed books, which are ~10% of
  archived observations.

### No model, rather than a fabricated one

`predict()` is a hand-weighted Binance model: three of its five terms
(`bid_imbalance`, `taker_imbalance`, `futures_basis_bps`) do not exist on
Kalshi, and on BRTI's scale `1.15 * 15` saturates the sigmoid so the
`model confidence >= 0.50` gate would pass on everything. It does not run.

There is no Kalshi-native probability model, so **none is reported**:
`raw_probability` is NULL and `bucket` is -1, meaning "no model". That
required relaxing two NOT NULL columns - and finding that `record()` inserts
with `OR IGNORE`, so the constraint was rejecting the row and the IGNORE was
swallowing it. Every prediction would have vanished: no settlement tracking,
no grading, no learning, and not one error anywhere.

### One feature contract, enforced

`feature_contract.py` hashes the definitions - source, units, cadence,
smoothing, both lookbacks, the cutoff rule and every band boundary - to
`fp=90a70cfa994e7a08`. Artefacts record the fingerprint they were fitted
under; `decide()` and `CandidateSet.evaluate()` refuse a mismatch and name the
differing field. Absent is not compatible: an artefact that will not say what
it was fitted under cannot be shown to match.

### What the live loop now does, verified end to end

Market `KXBTC15M-26SEP222315-15`, on the deployed code:

    23:04:10  decision recorded, DOWN @ 0.80, qualified, action=neutral
              context asia · mid · bd10-15 · px70-85
              reason  policy is keyed on retired features binance-1
              won=None            <- outcome unknown, as it must be
    23:08:14  decision recorded, DOWN @ 0.917, context moved to bd15+/px85-94
    23:15:18  settled and graded: won=1, on each row's own recorded side
              16 decisions, all DOWN, all graded

No candidate row: the frozen candidate speaks only to
`asia · mid · bd10-15 · px85-94` and this market was never in that cell. An
unmatched candidate writes nothing, because a table of non-opinions buries the
opinions.

## 48. Recovery sizing ends before the deficit is repaid (2026-09-23)

Recovery now stops UPSIZING when **both** hold:

    FOUR WINS   four profitable, fully closed markets since the cycle began
    HALFWAY     >= 50% of the cycle's INITIAL deficit recovered, net of fees
                and subsequent realised losses

Both, not either. Four wins that have barely moved the deficit leave real
ground to make up; half the money back after one lucky market says nothing
about whether the run is stable. 50% is the default, tunable within the
operator's 40-60% range - a value outside it raises rather than clamps,
because a threshold nobody intended is worse than an error.

**FOUR IS A FLOOR, NOT A CAP.** While recovery is under 50%, sizing continues
past the fourth win - past the fortieth. And because both conditions must
hold, this stops sizing LESS often than either alone would: it is more
restrictive about STOPPING, and therefore leaves the upsize on for LONGER.
An earlier version of this section called the AND "more conservative", which
is backwards on the thing that matters - time at exposure. "Both conditions"
reads like extra caution and does the opposite here.

**It is an exposure-reduction rule, not a prediction.** Nothing in
`recovery_exit` forecasts anything; it caps how long the account carries
doubled size.

### Ending is not repaying

The deficit is **preserved**. `active` now means "may upsize"; `owes` means
"money is missing"; they are different questions and the code answers them
separately. A base-only cycle keeps reporting what it owes, base-size wins
keep paying it down, and Telegram gets its own **RECOVERY SIZE ENDED**
message rather than the CLEARED one, which would have announced a $0.00
deficit that was not $0.00.

A loss during the base-only phase is recorded in full, does **not**
reactivate sizing, and does **not** reset the win counter - reactivating is
the loop the rule exists to break. Full recovery remains an immediate end in
its own right, even before four wins: there is nothing left to size for.
When the deficit truly reaches zero the cycle closes, and a later loss opens
a fresh one with its own count.

### Counting

A **market** counts once. Base and add-on fills on one ticker are one
position with one outcome, tracked in `recovery_cycle_wins` keyed by
(cycle, ticker) - counting realised *events* would reach four on two markets
that each settled twice. Wins need not be consecutive. Progress counts every
realised trade, base-size or upsized alike.

The denominator is the cycle's **initial** deficit, not its peak. A later
loss raises what is outstanding and so lowers the percentage, which is what
"net of subsequent realised losses" means.

### What it would have touched — a HYPOTHETICAL replay, not realised P&L

`scripts/measure_recovery_exit.py`, 154 realised events, 38 losses:

| | trades armed | net of those trades **as they ran** |
|---|---:|---:|
| recovery as it was | 150 | +14.89 |
| with the early end | 142 | +13.48 |

**Nothing here is realised improvement.** Ending sizing changes the QUANTITY
on the order; quantity changes the fill and the fee. The extra contract might
not have filled at all, and the fee on a different size is a different fee.
These figures are the P&L of trades as they actually ran, partitioned by
whether the rule would have upsized them. Supporting an estimate of what the
account would have made needs quantity-adjusted fills and fees, which this
does not attempt.

**8** trades would have changed size; their net as they ran was +1.42. That
says what those trades did — not what the rule would have earned or saved.

Two size-ends would have fired. The second is the operator's case exactly:
`KXBTC15M-26SEP230300-00` - **89 winning markets**, 54% of an $8.68 opening
deficit back, $3.97 still outstanding. An upsize riding 89 markets is the
exposure the rule is about, and no average-return measurement captures what
that costs when it breaks. It also shows the floor at work: the fourth win
did nothing, because the money condition was not met until the 89th.

### A migrated baseline is not the original loss

The cycle live at deployment opened with `initial = $0.1091`, which is a
deficit that was already in flight when the cycle columns arrived - a
**migration starting point**, not the loss that dug the hole. Seeding from
zero would have read as "100% recovered" and ended sizing on the first fold,
so the deficit in hand is adopted instead; but a percentage measured against
it is not progress against the original loss.

That distinction is recorded rather than remembered: `recovery_deficit.seeded`
marks an adopted baseline, it survives restarts, it resets when the cycle
clears, and the transition message appends "of the carried-over balance" so
the figure cannot be read as something it is not.

Deployed as instructed, with the measurement recorded beside it rather than
instead of it. Sizes and fills are not modelled - ending sizing changes size,
and a ledger of what happened cannot price what would have.

## 49. Continuous learning inside the service, and what it actually found (2026-09-23)

The intelligence layer had been stuck in a state nobody could tell apart from
working: a policy artefact loaded, decisions were recorded, `/learning`
rendered - and **every decision returned neutral for the same reason**, 928 of
them across 37 markets:

    reason                                          n     policy
    policy is keyed on retired features binance-1   928   v1-retired
    no evidence for this context                     84   v1
    policy is stale                                  84   v1
    pattern supports the existing decision            1   v1

The retirement guard was right and was doing its job. What was missing was the
replacement. This entry records building it, and reports separately what the
system now does and what the evidence actually supports - which are different
claims and only the first is a success.

### The loop runs in the service, not in a script

`scripts/train_brti_candidates.py` produced the previous candidate artefact.
A research script a person remembers to run is not a learning loop; it is a
habit, and habits lapse exactly when the market changes enough to matter. The
loop now lives in `src/btc15_signal/learning_runner.py` and is driven off the
service's own poll, immediately after the settlement sweep so it sees this
poll's evidence rather than the previous one's.

    trigger       when
    bootstrap     no valid policy is active - nothing else matters
    settlements   24 new settled markets (about six hours of a 96/day series)
    interval      6 hours regardless, so a quiet market still refreshes

Training runs in a worker thread with its own read-only connection: the fit
reads ~38,000 BRTI decision points and bootstraps 128 arms, and on the poll
thread that is a delayed fill. State - watermark, next due, last error,
consecutive failures - is persisted in `learning_state` and survives restarts.
A run left `running` by a killed process is closed out as `interrupted` on the
next startup, which is what stops a crash mid-fit from wedging the scheduler
into reporting "training in progress" forever.

**Three records, deliberately not one.** `learning_runs` says when training ran
and what data it used; `policy_activations` says when a policy became active;
`policy_withdrawals` says when an active adjustment was taken back and what
condemned it. They can disagree, and usually do: most runs fit a policy that is
never activated, which is the loop working, not failing.

### Kalshi only, and the 169 rows that proved it was not

Training reads `brti_decision_points` (Kalshi BRTI) priced on `contract_candles`
(the Kalshi book), plus the live `intelligence_decisions` graded against Kalshi
settlements and reconciled to Kalshi fills. `data/cohort.db` - the 6,428-market
corpus every earlier measurement in this file was computed on - is Binance
derived and is **not reachable from the module at all**.

The live leg needed the same treatment, and reading the `feature_version`
column was not enough to give it. That column is written from a module constant
so it says `brti-1` on every row ever recorded, including rows from before the
BRTI context fix whose keys are Binance (`dist<1.5`) or unlabelled (`? - ?`).
**169 live rows** declared `brti-1` over a key that meant something else - the
same failure the deployed policy artefact had, in the table the policy is
fitted from. Provenance is now read off the key itself
(`learning_data.brti_keyed`), positively: a key qualifies only by carrying a
BRTI distance band AND naming a real session and volatility regime, so a
malformed key, a Binance key and a future scheme all fail the same way.

    corpus     6,428 markets / 6,428 decisions
    live          44 markets /    53 decisions    (169 excluded, incompatible)
                                                  (16 excluded, unresolved)
                                                  (901 excluded, duplicate polls)
    TOTAL      6,466 markets / 6,481 decisions

Markets and decisions are counted apart throughout. 901 of those exclusions are
repeated polls of a window already represented: a market polled forty times is
one opportunity, and the row kept is the FIRST QUALIFYING poll, because that is
where the bot alerts and stops.

### What the fit found: two confidence arms, no execution adjustment

128 arms fitted. The bars, stated before the run and not moved after it:

    confidence    n >= 120, >= 2 days, day-clustered interval clear of zero
    execution     all of the above, PLUS validate n >= 40, sign agreement
                  between train and validate, and a validation interval
                  WIDENED by sqrt(candidates examined) still clear of zero,
                  and forward evidence that does not contradict it

    arms fitted                                   128
    eligible for confidence (n>=120, >=2 days)      7
    carrying a confidence adjustment                2
    ...that survive multiplicity widening           0
    examined as execution candidates                1
    PROMOTED                                        0

The two confidence arms are `asia - mid - bd<5 - px<70|reject` (n=129 over 34
days, -0.0985/ct, [-0.1486, -0.0255]) and `us - mid - bd<5 - px<70|reject`
(n=187, -0.0788/ct, [-0.1449, -0.0093]). Both are REJECT cells: they say the
rule's refusal of a sub-70c, sub-5x setup is well founded, and taking one would
have cost about 8-10c a contract. That is a useful thing to show and a safe one
to act on, because a confidence adjustment moves a label and can do nothing else.

**Why confidence is not held to the multiplicity bar, stated plainly.** The
widening corrects for CHOOSING: the policy looks at k cells, keeps whichever
points hardest against the base decision, and lets that one change an order -
which is k chances to be fooled. Confidence is not chosen; a delta is computed
for every eligible cell and which one is consulted is decided by where the
market puts the next signal. There is no selection to correct. The costs differ
in the same direction: a wrong confidence label means the operator reads
"lowered" on a setup that was fine, a wrong veto means a blocked winner, in
money. **Neither of the two arms would survive the execution bar, and the run
report says so in its own notes rather than leaving it to be discovered.**

### The Asia veto candidate: seven markets, one session, forgone profit

The one execution candidate is the same `asia - mid - bd10-15 - px85-94|accept`
veto that FINDINGS 46 froze. It remains **unpromoted**, and it fails on the
plainest possible criterion: validate n = 38 against a bar of 40.

Its forward record, asked for specifically:

    2026-09-23 03:30Z  UP   0.89  won  +0.1031
    2026-09-23 03:45Z  DOWN 0.85  won  +0.1410
    2026-09-23 04:30Z  UP   0.87  won  +0.1220
    2026-09-23 05:15Z  DOWN 0.89  won  +0.1031
    2026-09-23 06:00Z  UP   0.86  won  +0.1315
    2026-09-23 06:15Z  DOWN 0.86  won  +0.1315
    2026-09-23 06:45Z  DOWN 0.87  won  +0.1220

**Are the seven observations seven independent markets? Yes - and that is the
weaker half of the question.** They are seven distinct `window_open` values,
seven separate 15-minute markets, and `UNIQUE(window_open, candidate_id)` makes
a duplicate impossible by construction. But all seven fall in ONE Asia session
on ONE day, spanning 3h15m. A trend that carries five consecutive windows
carries their outcomes with it, so seven markets here are nothing like seven
independent observations, and the day-clustered bootstrap that every interval
in this file uses would treat them as roughly one. Seven markets, one day.

**The -0.8542 is an estimated opportunity cost, not a realised loss.** The rule
took all seven and won all seven; +0.8542 per contract is money that settled
INTO the account. The candidate would have stood aside and banked nothing. Its
forward figure is forgone profit, and `/learning` now labels it as such in
those words rather than printing a negative number next to a trading record.

### Two defects found in the wiring, both invisible to the suite

**`policy_line` was defined and never called.** The confidence delta had
nowhere to go: it was computed, stored and reported in `/learning`, and the
alert the operator actually reads never saw it. A layer that reports
"confidence lowered" beside a header still reading HIGH has not lowered
confidence; it has printed a sentence. The delta now enters `confidence_label`
on the same 0-100 points scale as the regime and clock terms, clamped with
them, so it moves the WORD - and `policy_line` renders beneath it saying why.

**`verdict` was two different things in one function.** `primary_signal` binds
the intelligence `Verdict` near the top and a plain log STRING further down on
four branches (`"eligible"`, `"no execution client"`, ...). Reading
`verdict.confidence_delta` at render time therefore raised `AttributeError` -
but only on the branches that reassign, which is why it passed a full 786-test
run before surfacing. Renamed to `intel_verdict`, with a source-level test
pinning the separation, because the failure is a name and not a value.

**And one that was there before either.** Applying a policy VETO or ADMIT to
`rule_match` consulted the policy's own `vetoes_enabled` flag and NOTHING else.
The two-switch design in `intel_mode` - `intelligence_mode` naming the
authority and `intelligence_authorised` granting it - existed, was documented,
and was never wired to the policy path. An artefact with `vetoes_enabled: true`
would have changed live orders with no operator authorisation anywhere in the
chain. `intelligence_policy.authorise()` now applies evidence and authority as
two separate gates, and records both: `final_action` is what took effect,
`evidence_action` is what the policy would have done with permission.


### Four more defects, found only by reconciling against the broker

None of these raised. Every one of them was a column that was always present,
always populated, and never right - the failure mode this project keeps
meeting. They were found by reading the learning loop's own output against
Kalshi's fills, which is the check the loop exists to make.

**`intelligence_decisions.ticker` was NULL on all 1,174 rows.** It was read as
`getattr(snapshot, "ticker", None)` and `MarketSnapshot` has no `ticker` - the
contract does. So the broker's fills and fees could never be joined to the
decision that caused them, and the learning loop scored **every executed trade
as a simulated one**. Now recorded from `contract.ticker`. The historical rows
are not rewritten: `predictions` recorded the same window's ticker correctly
throughout, so `learning_data.live_rows` resolves it at READ time through that
bridge and the archive keeps saying what it actually said. That recovered 25
executions from 0.

**`fills.fee_cost` is the fee for the WHOLE fill, not per contract.** 0.0294 on
two contracts at 70c is `0.07 * 2 * 0.7 * 0.3` - the published formula times
the COUNT. Using it per contract doubles the cost of every two-contract trade,
which is most of them under the current sizing.

**`yes_price` is not what a DOWN position cost.** A DOWN position is NO and the
fill row carries `no_price` explicitly; the first version derived it as
`1 - yes_price`, which is a guess in a place where the exchange has stated the
answer and is wrong wherever the pair does not sum to exactly 1. Related: a
SELL fill is an exit and was being priced as an entry, and a single fill was
matching BOTH sides of a window the model flipped inside - marking a decision
nobody executed as executed. Fills are now keyed on `(ticker, our side)`,
entries only, with exits kept apart and used for the realised figure.

**`intelligence_decisions.realised_pnl` is a placeholder.** The settlement loop
passes a literal `0.0` into `grade_intelligence`, so every graded row carries
`realised_pnl = 0.0` and not one of them means it. Reading it as money would
have scored **every real trade as break-even**. The realised figure for an
early exit is now computed from the broker's two fills - entry price, exit
price, both fees - and is `None` where no exit exists, so nothing claims a
number it does not have.

**And a gate name was stored one character at a time.** `failed_checks` is a
comma-joined STRING on both live paths. `intelligence_verdict` tested
`isinstance(failed_checks, list)` - a string is not one - and fell through to
`tuple(str(x) for x in failed_checks)`, which iterates CHARACTERS. "BRTI
distance" was archived as thirteen gates, `B, R, T, I, ...`. Nothing raised.
The consequence is that the ADMIT path, which may only rescue a setup whose
failing gates are exactly the one it names, **was dead by typo rather than by
decision** - it could never have matched. Fixed in `normalise_gates`, with a
test. Rows written before the fix keep their corrupted gate list; they fail
safe, because a garbled multi-gate signature refuses an admission rather than
granting one.

The corrected per-contract record over the 25 reconciled executions, priced at
the broker's fills and fees: the eight entries the current policy's feature
contract can key come to **-0.0038/contract** net - seven winners around
+0.12 and one loser at -0.85. That is the shape the whole system has: an edge
of about a cent against a loss that costs seventy.


### CORRECTION, same day: confidence was sized off the wrong quantity

The operator caught it: **negative profitability is not low directional
confidence.** An 89c contract that wins 89% of the time loses money on every
trade and is exactly as likely to win as the market says. The first version of
this work sized the confidence delta from dollars per contract, so it would
have shown "confidence lowered" on setups the market gets right nine times in
ten - a statement about price, dressed as a statement about the outcome.

The two arms it activated were precisely that error. `us · mid · bd<5 ·
px<70|reject` was selected at -0.1179/ct of *profit*; its *calibration* error is
-0.0515 with an interval of [-0.1182, +0.0174], which includes zero. The other
was -0.0229 [-0.0825, +0.0408]. Neither cell is measurably mispriced. They are
cheap contracts that lose money, which is a different fact.

**Confidence is now the calibration error and nothing else.** On Kalshi the ask
IS the implied probability that our side wins - paying 0.86 for a dollar payout
is the market saying 86% - so the quantity is `observed win rate - mean ask`,
measured per row as `won - ask` and bootstrapped by day. The structure is real
and it is the favourite-longshot bias, measured over 6,486 rows:

    ask ~0.4   n=128    wins 0.5234
    ask ~0.5   n=632    wins 0.5032
    ask ~0.6   n=1228   wins 0.5326
    ask ~0.7   n=1099   wins 0.6442
    ask ~0.8   n=2054   wins 0.8106
    ask ~0.9   n=1323   wins 0.9025

Profit still drives veto and admission, which are decisions about money.
Calibration drives the label, which is a statement about winning. A test pins
each: an expensive cell that wins gets no confidence change while its money
still points to a veto, and a cell that wins 90% at a 70c price does get one.

**And validation is now required for confidence too.** The earlier argument -
that a delta is computed for every eligible cell rather than the best of k
being picked, so there is no selection to correct - is still true and is still
why the multiplicity widening is not applied. But the operator is right that it
does not remove the need to validate. A confidence arm now additionally needs
`validate n >= 40` and the calibration error to hold its sign out of sample.

**THE RESULT OF THE CORRECTION: zero confidence arms.** All seven eligible
cells have a calibration interval that includes zero. The price is not
measurably wrong in any of them, which is exactly what FINDINGS 36 predicted -
the ask's Brier score of 0.2112 beat every model tried. So the live policy now
carries 128 arms, **0 confidence adjustments and 0 promoted arms**, and
`/learning` reports "adjusting confidence: no". The loop runs, ingests,
refits, evaluates and activates nothing. That is the honest state.

### CORRECTION: executions, counted once

"0 real fills became 25" in the section above was quoted from an intermediate
build that matched any fill to any decision by ticker alone. Two things were
wrong underneath it.

**`fills.side` does not reliably name the leg we held.** This account has
entries booked `buy/yes` AND entries booked `sell/no`, and a cash-out of a YES
position reported as `sell/no` carrying `yes_price` 0.997. Any reading of our
position from that field is a guess, and a guess about which side we were on
inverts the trade.

**An execution is not a fill.** 19 of this account's market-sides have two to
four buy fills - a base order plus recovery add-ons. Folding them wrongly
under-counts the contracts and the fees; counting each as its own trade reports
one opportunity as three.

Both are solved by reading the position from `/portfolio/settlements`, which
states `yes_count`/`no_count`, `yes_cost`/`no_cost` and `pnl` outright - the
same source every other money figure in this system already comes from, and the
project's own rule: read P&L from Kalshi, never rebuild it. A cashed-out market
shows BOTH counts, because Kalshi books an early exit as buying the opposite
side; the leg we opened is the expensive one and the size is the netted pair,
not their sum. The fills table is now used for exactly one thing: how many
fills an execution took.

The corrected figures, and they reconcile exactly:

    executions                     25   (one per market-side, not per poll)
    contracts                      47
    decision rows behind them      25   (the 13:00Z market alone was 25 polls)
    sum of per-contract P&L x size  +3.3803
    sum of settlements.pnl          +3.3803   <- agrees to the cent

### CORRECTION: a market could span two dataset splits

`chronological_split` cut the row list at an index. A window the model flipped
inside contributes an UP row and a DOWN row, so the boundary could land between
them and put one market's outcome in both the fit and the check of the fit.
Invisible - both slices look the right size - and it flatters exactly the cells
that contain flipped windows. The boundary now falls between MARKETS and every
row of a window travels with it; the slice sizes are approximate instead of
exact, which is the correct trade.


### The method needed a version too, and the guard paid for itself immediately

Correcting confidence from profit to calibration left a live artefact whose
two deltas had been fitted by the superseded method. Nothing would have caught
it: `feature_version` still said `brti-1`, the fingerprint still matched, and
the deltas were integers in the same field they had always been in. The
corrected build would have gone on applying the old method's numbers to live
decisions until the next scheduled refit hours later.

So the METHOD is versioned like the features. `feature_version` says what the
numbers ARE; `model_version` says what was DONE to them, and they fail the same
way - by looking applied.

    arms-shrunk-2      confidence sized from profit
    arms-calibrated-1  confidence sized from the calibration error, validated
                       out of sample

`policy_is_valid` now refuses a superseded artefact beside the retirement guard
and the feature check, and the refusal makes a rebuild due. On the next start
it did exactly that, unprompted:

    learning: active policy CANNOT ACT - fitted by superseded method
              arms-shrunk-2 (current arms-calibrated-1)
    learning: training run 2 started [bootstrap] over 43 settled markets
    learning: run 2 ACTIVATED - 6471 markets, 128 arms, 0 with confidence,
              0 promoted

### AN OUTAGE I CAUSED: 2026-09-23 13:54:28Z to 13:57:01Z

Verifying the corrections took seven service restarts inside one hour.
`watchdog.py` has `MAX_RESTARTS_PER_HOUR = 6`, and on the seventh it did
exactly what it is built to do: stopped, sent `WATCHDOG STOPPED`, and left
nothing running. **The service was down for two and a half minutes with no
supervisor.** Only the recorder survived; the watchdog had to be started by
hand, and it brought the service back up itself.

Nothing was lost - no signal qualified in that window and the open position
lives at Kalshi and settles regardless of whether this process is up - but
that is luck, not design.

**The lesson is about deployment, not about the watchdog.** The cap is correct
and it protected the account from a restart loop. What was wrong was treating a
live trading service as somewhere to iterate: each fix was small, each restart
looked free, and the sixth was indistinguishable from the first. A restart
budget is a real resource and it is spent silently.

**How to apply:** batch changes and deploy once. Before any restart, count the
restarts already made in the last hour - `runtime/watchdog.log` and the process
creation times both show them - and if the count is near six, stop and wait out
the hour rather than spending the last one. If the watchdog is gone, start THE
WATCHDOG, not the service: it takes the lock and brings the service up itself.


### CORRECTION: attribution, through the broker's own fill and order ids

Matching an aggregate is not attribution. The +$3.3803 total agreed with
`SUM(settlements.pnl)` while the rule deciding WHICH DECISION each trade
belonged to was a heuristic: "the leg that cost more is the one we opened".

It fails in the worst place. Buy YES at 0.80, watch it fall, cash out by buying
NO at 0.85, and the NO leg is the expensive one - so the heuristic reports the
position we exited INTO as the position we took. Measured over this account's
**60 closed pairs it misattributes 6**, including a -$1.75 loser whose side it
inverts.

Attribution now runs off the fills, in time order, and is CHECKED:

    first fill chronologically   the entry; its leg is our side
    later fills on that leg      adds (the recovery add-ons)
    fills on the opposite leg    the exit - Kalshi books a close as buying
                                 the other side
    fill_id / order_id           carried onto the row, so any figure traces
                                 back to the executions behind it

Rebuilding `yes_count`/`no_count`/`yes_cost`/`no_cost` from the fills must
reproduce the settlement row. **That check is what makes this attribution
rather than another guess**, and it settled the convention empirically rather
than by reading: every fill acquires `count` contracts of its named `side` at
that side's price, whatever `action` says - which reproduces the exchange's
totals on **154 of 154** settled tickers, against 61 of 154 for reading
`action` as a signed direction.

Where the reconstruction does not reconcile the market is UNRESOLVED and is
**excluded from execution-based learning entirely** - not quietly demoted to a
counterfactual, because something really did happen there.

    executions            26  (was 25; one more resolved by fill order)
    contracts             49
    multi-fill executions  2  (one with 2 adds, one with 1)
    unresolved             0
    net per-contract x size  +3.7196  ==  SUM(settlements.pnl)

### CORRECTION: confidence calibrates OUR model, not Kalshi's price

The previous pass compared the observed win rate with the ASK. The ask is the
MARKET's prediction, so that measures whether Kalshi is priced correctly - a
real question, and not the one a confidence label answers.

`confidence_label` scores a setup out of 100 from its passing gates and the
time of day. That score is the prediction we display, so it is the prediction
that has to be compared with outcomes. It is now **recorded on every decision**
(`intelligence_decisions.model_points`) before any learned adjustment touches
it, and reconstructed for the corpus through the SAME function the live path
calls - `regime.model_points`, one definition, both callers.

That parity is not decorative. The first attempt passed `0` for the protective
level term, where the live path calls `level_points(False)` and gets **-6**, so
every historical row scored six points above the live one and the "calibration"
was a comparison between two implementations. A test now pins the two
expressions equal.

**The model's score is informative, and unevenly so** - the reliability curve
over the training slice, which is published on the artefact:

    points 0-69    0.54 - 0.63      essentially flat: no information
    points 70-79   0.77
    points 80-89   0.79
    points 90-100  0.86

So the upper half of the score carries real signal and the lower half carries
almost none. That is worth knowing and is invisible if the only thing ever
compared with outcomes is the price.

**The two calibrations disagree, which is the point.** For
`us · mid · bd10-15 · px85-94|accept`:

    model  : predicted 0.840, observed 0.893, gap +0.0529 [+0.0061,+0.0979]
    market : ask       0.885, observed 0.893, gap +0.0076 [-0.0404,+0.0538]

Kalshi has this cell priced about right. OUR confidence score understates it by
five points, out of sample as well (validate n=59, +0.1462, same sign). So this
build carries **one validated confidence adjustment: +5 points** in that cell -
earned against the model's own prediction, with the market comparison recorded
beside it and never applied.

**An interval spanning zero is insufficient evidence, not a finding.** The
wording now says so explicitly: "INSUFFICIENT EVIDENCE at n=..., not a finding
that the score is correct". The earlier text claimed the price "is not
measurably wrong here", which reads as a result and is not one.

`arms-calibrated-1` is retired with the rest: an artefact whose deltas were
sized against the ask cannot act.


### CORRECTION: a 61-point score is not a 61% prediction

The previous entry described the active arm as "our score is overconfident
about those setups by nine points", which reads as a nine-percentage-point
probability correction. It is not one, and the operator was right to stop it.

`confidence_label` produces a HEURISTIC SCORE out of 100 - passing gates, time
of day, protective level. The mapping from that score to a win frequency is
LEARNED, from the training slice, and everything downstream treats its output
as a probability. That is an empirical claim, and it had never been checked on
data the mapping did not see.

**So it was checked.** Fit on train, applied unchanged to the validation slice
and to the holdout, which no fitting has ever touched:

                          VALIDATE (1,618)   HOLDOUT (1,250)
    Brier, curve               0.1896            0.1839
    Brier, base rate           0.2103            0.2058
    ordering preserved         6/8 buckets       5/7 buckets
    mean |predicted-observed|  0.0358            0.0360

**The score RANKS out of sample and its LEVEL does not.** Beating the base rate
on both slices is a real result: the heuristic carries genuine information
about winning, which is worth knowing and was not previously established. But
the average calibration error is ~3.6 points and the bias is one-directional -
nine of ten validation buckets and eight of ten holdout buckets came in BELOW
their prediction. The mapping over-predicts.

**And that is most of the active arm.** `us · mid · bd<5 · px<70|reject` has a
gap of -0.0902. Its rows sit almost entirely in score buckets 30-69, and the
curve's own out-of-sample error IN THOSE BUCKETS is:

    bucket 30-39   validate -0.0417   holdout +0.0034
    bucket 40-49   validate -0.0534   holdout -0.0986
    bucket 60-69   validate -0.0685   holdout -0.1147

So a cell living in the 30-69 band should show a gap of roughly -0.04 to -0.11
whether or not anything is special about it. Against the seven cells with
n>=120 the weighted mean gap is -0.0220 and this one is -0.0682 below that -
but measured against the curve's bias in its OWN buckets it is not clearly
distinguishable from the mapping's systematic over-prediction.

**The arm's out-of-sample check does not rescue this**, because it applies the
same train-fitted curve to the validation slice. The curve's bias is present
identically in both, so "validate agrees" confirms the bias reproduces, not
that the cell is special.

**What the -9 therefore is, stated correctly:** a CONFIDENCE-SCORE ADJUSTMENT
of nine points on the 0-100 scale, applied to a cell that sits in a score band
the learned mapping over-predicts. It is not a demonstrated nine-percentage-
point probability correction for that cell. It is label-only, it reaches no
gate, no order and no size, and the worst it can do is render a refused signal
LOW where it would have read MEDIUM.

**What would fix the method** (next release, not this one - the service is
deliberately being left alone):

  * de-bias the mapping against held-out data rather than fitting it in-sample
    and trusting the level, or
  * measure each cell's gap as a RESIDUAL against a curve calibrated on data
    the cell's own validation slice did not contribute to, so the global bias
    cancels instead of being attributed to whichever cells occupy the biased
    band.

Either way the bar should then be "this cell deviates from the mapping's own
behaviour in its band", which is the question the adjustment is supposed to be
answering.

**A working learning system, a validated adjustment and an improvement in
predictions remain three separate claims.** The first is deployed. The second
is now weaker than the previous entry stated. The third is not claimed at all.


### And the -9 does not survive a correct out-of-sample treatment

The previous section established that the points -> frequency mapping
over-predicts out of sample, and that the active arm lives in the band where it
over-predicts most. That raised the obvious question, so it was measured: does
the cell deviate from the mapping's OWN behaviour in its band, once the
mapping's level error is removed?

Three slices, used once each and in order, so nothing grades its own homework:

    TRAIN     fit the points -> frequency mapping        (3,560 rows)
    VALIDATE  measure the mapping's per-bucket BIAS      (1,619 rows)
    HOLDOUT   measure each cell's RESIDUAL gap against
              the de-biased mapping                      (1,311 rows)

**The mapping over-predicts in every single bucket**, by a mean of -0.0516:

    bucket 10-19  -0.1015     bucket 60-69  -0.0685
    bucket 20-29  -0.0656     bucket 70-79  -0.0439
    bucket 30-39  -0.0417     bucket 80-89  -0.0133
    bucket 40-49  -0.0534     bucket 90-99  -0.0157
    bucket 50-59  -0.0609

**Drift is only part of it.** The base rate fell 0.7199 -> 0.6998 -> 0.7109
across the three slices (accepted leg 0.8584 -> 0.8282 -> 0.8357), so about one
to two points of the bias is the market moving, which periodic refitting
tracks. The remaining three to four points is NOT diagnosed. Shrinkage optimism is
one candidate; regime change beyond the base-rate shift, bucket instability
and composition change between slices are others, and nothing measured here
distinguishes them. See the correction below.

**With the bias removed, NO cell clears zero on the holdout** - including the
active one:

    -0.0483 [-0.1427,+0.0470] n=71   us · mid · bd<5 · px<70|reject   <== active
    -0.0265 [-0.1355,+0.0965] n=65   asia · mid · bd<5 · px<70|reject
    -0.0081 [-0.0731,+0.0651] n=58   us · mid · bd10-15 · px70-85|accept
    +0.0026 [-0.1109,+0.0935] n=45   asia · low · bd10-15 · px70-85|accept
    +0.0145 [-0.0776,+0.1118] n=46   us · mid · bd10-15 · px85-94|accept
    +0.0362 [-0.0676,+0.1312] n=59   europe · mid · bd<5 · px<70|reject
    +0.0370 [-0.0565,+0.1229] n=73   asia · mid · bd10-15 · px70-85|accept
    +0.0414 [-0.0747,+0.1399] n=57   europe · mid · bd10-15 · px70-85|accept

So the arm's -0.0902 decomposes into roughly -0.05 of mapping bias and -0.05 of
cell residual, and **the residual is not distinguishable from zero** at holdout
sample sizes (n=71, interval spanning zero by a wide margin).

**THE ONE ACTIVE CONFIDENCE ADJUSTMENT IS THEREFORE NOT WELL FOUNDED.** It is
live, it is label-only, it cannot reach a gate, an order or a size, and the
worst it does is render a refused signal LOW where it would have read MEDIUM.
But it should not be described as evidence of anything, and the next scheduled
run will re-derive something like it under the same method - so a scheduled run
producing an adjustment is the LOOP working, not the adjustment being
validated. Those must not be read as one event.

**The fix, for the next release** (the service is deliberately being left
alone):

  * de-bias the mapping against held-out data instead of trusting a fitted
    level, and
  * require a cell's gap to clear zero as a RESIDUAL on a slice that
    contributed to neither the mapping nor the bias estimate.

On today's corpus that bar admits nothing, which is the correct outcome: eight
cells have enough holdout evidence to be tested and none of them deviates from
the mapping. The honest position is that the confidence layer has no validated
adjustment, and the previous two entries each claimed one on a weaker test than
this.


### The -9 is withdrawn, and the confidence bar is rebuilt

Two things were wrong and the operator named both.

**"Label-only" does not license displaying an unsupported number.** The -9 was
left live on the grounds that it could not reach an order. That is true and it
is not the point: the confidence label is shown to a person, and a number the
analysis no longer supports should not be on the screen whatever it cannot
reach. It is withdrawn.

**A correction cannot wait behind a self-imposed rule.** "Avoid repeated
restarts" was about churn, not about preserving a known defect. Hot reload
could not do it - `deteriorated()` covers promoted EXECUTION arms on forward
evidence, and nothing outside the runner can drop the cached policy - so this
went out as one tested corrective deployment.

### The corrected method: nested chronological folds

The old bar could not separate a cell from the mapping. It measured a cell's
gap against a curve fitted in-sample and checked it on a validation slice using
THAT SAME CURVE, so the curve's level error was present identically in both and
"validate agrees" confirmed the error reproduced rather than that the cell was
distinctive.

Now every prediction comes from a mapping that saw only earlier data, de-biased
on a slice earlier still than the one being tested:

    fold k    curve fitted on folds 0..k-2
              per-bucket bias estimated on fold k-1
              cell residuals measured on fold k

Five folds cut by MARKET, three of them tested, 3,841 scored rows. Nothing
grades its own homework and the result does not rest on any single holdout -
which matters, because a holdout once inspected is spent, and the one from the
previous entry is now evaluation evidence rather than a test set.

### Multiplicity applies to confidence after all

The previous entry argued it did not: a delta is computed for every eligible
cell rather than the best of k being picked, so there was said to be no
selection to correct. **That was wrong**, and the corrected method is what
exposed it - a significance test decides WHICH cells get a non-zero delta, and
keeping whichever of k cells clears an interval is k chances to be fooled
however many were looked at.

Under the nested test, 7 cells had enough evidence and 2 cleared zero raw:

    +0.0620 [+0.0090,+0.1112] n=142  us · mid · bd10-15 · px85-94|accept
    -0.0780 [-0.1420,-0.0235] n=185  us · mid · bd10-15 · px70-85|accept

Against 0.35 expected by chance at 95% across 7 cells, that is suggestive and
no more. **Neither survives the sqrt(7) widening** the execution bar has always
applied, and neither is activated. The widening was added on discovering the
selection - which makes the bar stricter. Relaxing one after seeing a result
would be the other thing, and is what this file exists to catch.

The old -9 cell now reads `-0.0510 [-0.1141,+0.0080]`: it spans zero even
before the widening.

**Result: 128 arms, ZERO confidence adjustments, zero promoted.** The
confidence layer has no validated adjustment and the artefact says so.

### A correction to the previous entry's causal claim

It said the residual bias, after drift, "is in-sample optimism: each bucket is
fitted to its own noise and pays for it out of sample". **That is not
established.** What the comparisons show is out-of-sample miscalibration -
the mapping over-predicted in all nine buckets, mean -0.0516, while the base
rate fell 0.7199 -> 0.6998 -> 0.7109. Shrinkage optimism is one candidate
cause. Regime change beyond the base-rate shift, bucket instability, and
composition change between slices are others, and nothing measured here
distinguishes them. The claim is withdrawn to: **the mapping's level is wrong
out of sample by roughly five points, and why is not determined.**

That distinction is not pedantry. "Optimism" implies the fix is shrinkage;
"drift" implies the fix is refitting; "composition" implies the buckets are
wrong. The corrected method de-biases against held-out data, which helps under
all three, and that is the honest reason to prefer it.

### What is and is not claimed

    the loop runs, ingests, refits, evaluates, activates      DEPLOYED
    an adjustment has earned the right to change a label      NO
    an adjustment has earned the right to change an order     NO
    the confidence score carries information about winning    YES, as a
                                                              RANKING: Brier
                                                              0.1839-0.1896
                                                              against a base
                                                              rate of
                                                              0.2058-0.2103
    that score's LEVEL is a probability                       NOT SHOWN
    intelligence improves signals                             NOT CLAIMED

The next confidence adjustment needs fresh forward evidence strong enough to
clear a nested, selection-corrected interval. On today's corpus nothing does,
and that is the correct state rather than a disappointing one.


### The context key has the subject and the context the wrong way round

The operator's framing: the layer exists to learn **which matching setups
deserve stronger confidence, which produce losses, and which rejected setups
should qualify.** Session and volatility regime are supporting context, not the
strategy.

The deployed key is `session · vol_regime · distance · price` - context first,
setup last. Measured over 6,491 decisions on 6,475 markets:

    keying                            cells  n>=120  decisions covered
    session · vol · distance · price   139     18     3,754  (58%)
    session · distance · price          60     17     5,363  (83%)
    vol · distance · price              41     15     5,882  (91%)
    distance · price  (setup only)      16      8     6,352  (98%)

**Session and regime fragment the ASSEMBLED TRAINING DATASET and leave 42% of
its decisions in a cell too small to speak.** That dataset is 99.3% backfilled
archive; the live system's own record is 1,376 decisions over 56 markets. See
the population correction below. The largest deployed cell
holds 345 decisions; the largest setup-only cell holds 1,798. That is the
symptom this file has been circling - almost nothing clears any bar - and a
large part of it is the partition, not the market.

**But re-keying does NOT change today's answer**, and it is worth being exact
about why:

    keying                     eligible  clear raw  survive multiplicity
    session · vol · dist · px      7         2              0
    distance · price               7         5              0
    vol · distance · price         9         6              0
    session · distance · price    11         4              0

Five of seven setup-only cells clear zero raw against two under the deployed
key, so the extra evidence is real. The `survive multiplicity` column used
sqrt(k), which is NOT a valid correction - see the correction below, where a
proper Holm-Bonferroni test rejects one cell under the deployed keying. The binding constraint is **independent DAYS, not markets**:
the interval is bootstrapped by day, so a cell with 1,798 decisions spread over
the same ~70 days is barely narrower than one with 345. More markets per cell
does not buy what it looks like it buys.

**AND CHOOSING A KEYING BECAUSE IT CLEARS MORE WOULD BE THE SELECTION THIS
ENTRY JUST FINISHED CORRECTING.** Four schemes have now been looked at. The
keying must be chosen on what the layer is FOR - which is the operator's
argument and it is sufficient on its own - and then evaluated on evidence that
arrives afterwards. It must not be chosen on which partition happens to light
up on data already seen.

So the change is declared here, before it is evaluated:

  * the context key becomes SETUP-FIRST. Distance band and price band are the
    subject; session and volatility regime are recorded alongside every
    decision as context and are available for reading, but do not partition
    the evidence by default.
  * the bar is unchanged - nested chronological folds, interval clear of zero,
    multiplicity widened across eligible cells. Fixed before the re-keying, not
    after seeing what it admits.
  * historical live rows stay usable: their recorded key is session-first and
    the setup key is recoverable by dropping the leading components, so no
    archive is rewritten and no decision loses its provenance.

Not shipped in this release. The current build is safe - zero adjustments, and
nothing is being acted on - so it goes out after the pending scheduled-training
proof rather than resetting that clock for a third time.


### CORRECTION: sqrt(k) is not a multiple-comparison correction, and it hid a result

The previous two entries reported "neither survives the sqrt(k) widening" and
"none survives any scheme" as though that settled significance. It does not.
**sqrt(k) widening of a bootstrap interval is ad hoc** - it has no stated
coverage guarantee, it is not a test, and reporting its verdict as a finding
overstates what was established.

Replaced with two established corrections on a two-sided day-clustered
bootstrap p-value (4,000 draws, resampling days):

    Holm-Bonferroni     controls the family-wise error rate - any false
                        positive at all. The conservative choice, and the right
                        one when a single false positive puts a wrong number on
                        the operator's screen.
    Benjamini-Hochberg  controls the false discovery rate. Reported beside it
                        because it is the usual choice for screening many
                        cells.

The seven cells with nested out-of-sample evidence, worst p first:

    cell                                      n     mean       p   Holm   BH
    us · mid · bd10-15 · px70-85|accept     185  -0.0777  0.0055   YES   YES
    us · mid · bd10-15 · px85-94|accept     142  +0.0625  0.0225    no    no
    us · mid · bd<5 · px<70|reject          209  -0.0504  0.1230    no    no
    asia · mid · bd<5 · px<70|reject        168  -0.0431  0.3035    no    no
    europe · mid · bd10-15 · px70-85|accept 154  -0.0325  0.3250    no    no
    europe · mid · bd<5 · px<70|reject      146  -0.0215  0.5515    no    no
    asia · mid · bd10-15 · px70-85|accept   216  -0.0119  0.6920    no    no

**ONE CELL SURVIVES A PROPER FAMILY-WISE CORRECTION.**
`us · mid · bd10-15 · px70-85|accept` at p = 0.0055 against a Holm threshold of
0.05/7 = 0.0071, over 185 nested out-of-sample observations across 38 days. The
second candidate fails both corrections (Holm threshold 0.0083, BH threshold
0.0143, against p = 0.0225).

sqrt(7) = 2.65 rejected both, which is roughly a 99.6% interval - so the ad hoc
rule was not merely unjustified, it was far more conservative than the
correction it stood in for, and it suppressed a result that a stated method
supports. That is the same class of error as an unjustified activation, in the
other direction, and it is worse for being reported as though it were rigorous.

**What this does and does not establish.** It is evidence that the confidence
SCORE deviates in that cell - it wins about 7.8 points less often than the
de-biased mapping predicts. It is not a probability correction, the mapping's
level is still only validated as a ranking, and Holm accounts for the seven
cells but NOT for the four keying schemes that have now been examined. That
outer layer of selection is uncorrected, which is a further reason the
setup-first keying had to be declared before evaluation rather than chosen
after it.

Nothing is activated on this. The next release evaluates the declared
setup-first keying under Holm, and whatever clears there is what gets decided
about.

### CORRECTION: two populations, and they are not the same

The coverage table said "42% of every decision the live system has taken" sits
in cells too small to speak. Wrong twice: it describes the ASSEMBLED TRAINING
DATASET, and that dataset is 99.3% backfilled archive.

    POPULATION 1 - the live system's own record
      intelligence_decisions   1,376 rows over 56 markets (all policies)
      of which BRTI-keyed      1,207 rows over 48 markets

    POPULATION 2 - the assembled training dataset
      archive (backfilled BRTI, priced on Kalshi candles)   6,428 markets
      live (one row per market-side, settled)                  48 markets
      TOTAL                                    6,476 markets / 6,493 rows

The coverage figures are about Population 2. The live system has taken
1,376 decisions, not 6,493, and the fragmentation finding is a statement about
what the fitter can learn from the assembled corpus - which is the right thing
to say, but it has to be said in those words.

### What is live, and what is not

    running                          YES  - scheduled, persisted, restart-safe
    updating                         YES  - refits on settlements or 6h
    adjusting confidence             NO - the -9 was withdrawn and the bar
                                            rebuilt on nested out-of-sample
                                            folds with multiplicity applied.
                                            Nothing on today's corpus clears
                                            it.
    authorised to affect execution   NO   - 0 arms cleared the evidence bar,
                                            and the operator's two switches
                                            are not set either

Those are four different claims and `/learning` now shows them on four lines.
A system can be running, updating and adjusting confidence while being
authorised to change nothing, and that is exactly the state here.

**A working learning system and a proven profitable adjustment are separate
claims.** The first is built, deployed and verified. The second is not made:
no adjustment earned activation, and the honest reading of why is that this
account has 44 live markets of Kalshi-native forward evidence against the
~10,700 qualified signals FINDINGS 38 estimated a veto would need. The loop now
accumulates that evidence by itself, which it previously could not.

### Superseded, and what survives

Everything in FINDINGS 1-40 computed on `cohort.db` or the `market_data.db`
Binance columns is **superseded as a basis for the live policy** - not
withdrawn as a measurement. Those numbers are still what they were; they simply
describe an instrument the system no longer trades on, which disagrees with the
official settlement on 19.4% of outcomes (FINDINGS 41). The originals are
preserved in place, and the artefact that was fitted on them is kept as
`runtime/intelligence_policy.binance-v1.retired.json`.

What that does NOT overturn: FINDINGS 36 (no model beats the ask) and
FINDINGS 37 (every implementable wait policy loses) were paired, same-market
measurements where the feed error largely cancels, and nothing here contradicts
them. FINDINGS 43's warning that thresholds do not transfer between instruments
is the reason the BRTI bands exist at all and is reinforced, not superseded.
FINDINGS 44-48 concern sizing, recovery and execution controls; they are
untouched by this work and remain binding - no mode may change size, and
`intel_mode.may_change_size` returns False for every mode with a test asserting
it for each.

## 50. Setup-first keying, and four defects that a full suite could not see (2026-09-23)

Four releases shipped on 2026-09-23: setup-first learning (`brti-2`), the
shared message surface, a delivery-tracking hotfix, and position
reconciliation. Each was found by a different method, and only one of the four
by a test.

### The intelligence keys on the SETUP

Arms key on distance band, price band and aligned momentum. Session and
volatility regime are recorded beside every decision and key nothing.

| keying | cells | at n>=120 | coverage |
|---|---|---|---|
| context-first (`brti-1`) | 139 | 18 | 58% |
| setup-first (`brti-2`) | 30 | 13 | 93% |

Session was splitting one setup's evidence four ways and calling the
fragments different setups. The live policy `kalshi-brti-2-*` fits 29 arms
over 6,428 markets.

### TWO BARS, and they are not the same measurement

This was reported once as a contradiction. It is not; the two tests differ in
quantity, family and purpose, and the report note used the word "survives"
for both.

| | CONFIDENCE | EXECUTION |
|---|---|---|
| quantity | nested out-of-sample **calibration residual** (observed win rate minus the probability the price implied) | **P&L**, day-clustered, on the validation slice |
| method | nested chronological cross-validation, 3 folds | day-clustered bootstrap |
| correction | Holm-Bonferroni, FWER 0.05, 11 testable cells | Holm-Bonferroni, FWER 0.05, examined cells |
| result | **3 arms pass** (p = 0.005, 0.002, 0.001) | **none passes** |
| can it move an order? | no - it re-rates a displayed word | yes, and only with both switches on |

The execution bar was still using `sqrt(k)` interval widening after the
confidence path moved to Holm - so the bar deciding whether a cell may change
an ORDER was the one still using a widening with no stated coverage. Both now
use the same stated correction and `_survives_widening` is deleted. On the
live corpus this changed nothing operationally: 2 cells examined, 0 survive,
0 promoted. Each arm now carries `delta_method`, `delta_correction`,
`delta_p`, `delta_cells_tested`, `delta_folds` and the execution bar's own
`execution_p` as FIELDS rather than as a sentence inside `delta_reason`.

### Binance was still running, and it was not the client

The guards covered the network client and the policy artefact.
`archive_observation` was calling `predict()` and `EntryRule.matches()` on
**every poll**: Binance-fitted arithmetic over a BRTI-built snapshot, written
to `side`, `raw_probability`, `bucket`, `distance_bps` and
`normalized_distance` - column names that carry no instrument.

    hour before the fix   296 archived rows, 296 with a Binance probability
    after the fix         0

It is also what crashed the service every fifteen minutes between 04:04 and
05:04: `strategy.py` compares `prediction.raw_probability >=
min_raw_probability` and a Kalshi prediction has none, so `None >= float`
raised TypeError - inside an `except Exception`, so it failed silently and
succeeded harmfully. Guard the MODEL and the RULE, not just the client.

### A column that reached every fresh install and no live database

`notifications.status` shipped inside `CREATE TABLE IF NOT EXISTS`, which adds
nothing to a table that already exists. `begin_delivery` is the first
statement `send_once` runs and sat OUTSIDE its try block, and the fill site
catches `(httpx.HTTPError, OSError, RuntimeError, ValueError)` - of which
`sqlite3.OperationalError` is none. The first message after the deploy would
have raised straight out of the poll loop with a position open. Every test
that builds its database from the current DDL passes regardless; the
regression tests now start from the byte-for-byte pre-migration schema.

### Seven filled orders recorded as cancelled

A recap read "Bought DOWN at 85c / Cost $1.72 / Profit +$0.45". Two contracts
from 85c to 99.7c is 29.4c gross, so +$0.4456 could not come from it. The
position was really three contracts:

| order | leg | qty | price | fee | type |
|---|---|---|---|---|---|
| `01a0cfde-71e8-` | base | 2 | 0.85 | 0.0179 | taker |
| `01a0cfde-79b8-` | recovery add | 1 | 0.83 | 0.0 | maker |
| `01a0cfe6-e9e0-` | exit | 2 | 0.997 | 0.0005 | taker |

3 bought for 2.5479, 2 sold for 1.9935, 1 run to settlement for 1.00 =
**+0.4456**, and Kalshi's settlement record agrees to the cent
(`no_count_fp 3.00`, `no_total_cost_dollars 2.530000`).

`order_status` read `/portfolio/events/orders/{id}`. CREATE and CANCEL
legitimately moved to that family; the READ never existed there and returns
404 for every order. So `_bank_if_filled` never banked anything and the
cancel/fill race it was written for had **never once** been resolved. Across
two days, **7 of 7** recovery adds that filled were recorded as CANCELLED,
`cancel_reason` reading "order not found (already filled, expired or
cancelled)" - true, and read as its opposite. `recovery_add_budget` was an
empty table: $0 charged where $5.53 had gone out.

The field names never matched either. Kalshi sends `fill_count_fp`,
`maker_fill_cost_dollars`, `taker_fill_cost_dollars`, `*_fees_dollars`; the
parser read `taker_fill_count`, `average_fill_price_dollars`,
`fees_paid_dollars`, and fell back to `yes_price_dollars` - the COMPLEMENT on
a DOWN leg, 0.1700 for a fill at 0.8300. **Take the price as cost / count.**

**Why a full suite passed.** The test doubles returned the invented names, the
parser read them, and the two agreed with each other and with nothing else.
Order fixtures are now built from a captured live response.

**The ledger was correct throughout**, because it reads
`/portfolio/settlements`. The money was right and the message was wrong,
which is the only ordering of those two that is recoverable.

### The footer mixed two quantities under one label

`Today` printed `realised + open_mark`, so on the poll where a position marked
to zero the dollars moved while the count did not, and a recap announcing a
loss sat above totals that had absorbed the mark but not the settlement.
`Today` is realised only; an open position has its own line labelled as a
mark; a recap whose market the broker has not settled says its totals are as
of the last reconciliation.


## 51. Recovery read as two subsystems contradicting each other (2026-09-23)

The operator, on three consecutive Telegram messages:

    16:58  RECOVERY SIZE ENDED / 4 winning trades - 91% recovered
           $0.16 still outstanding / Continuing at normal base size.
    17:00  Recovery add: pending - rested at 82c
           Recovery: $0.16 outstanding - base size only

*"One message says recovery has ended because he has already exceeded the
50%. Another said still active."*

Both readings were reasonable. One of the two lines was false.

### The add never rested. It was never placed.

`KXBTC15M-26SEP231700-00`, the row behind that recap:

    state         RECOVERY ADD SKIPPED
    order_id      NULL
    placed_ms     NULL
    limit_price   0.82
    cancel_reason crossing history unavailable for this window

The 82c was the price the add WOULD have rested at. Nothing was sent to the
exchange.

**Root cause.** `position_for_window` derived the leg state by elimination:

    filled if filled_count, else cancelled if cancelled_ms, else "pending"

There is no case for a refusal, and a refusal sets none of those fields - so
it fell to the `else`. **46 of the 57 adds on record are refusals**, so the
commonest outcome was the one reported wrongly, and it was reported as the
one state that implies live money in the book.

The state now comes from the record, not from silence: `placed_ms IS NULL`
means never placed, and a leg that was never placed is `skipped` and is
rendered with its reason rather than a price it never traded at.

**Why a full suite passed.** Both tests that touched this built the add with
`RECOVERY ADD PENDING`. The 80%-of-rows case had no fixture at all.

### Size ended and still outstanding are the same fact, spelled two ways

`surface.recovery_line` printed `Recovery: $0.16 outstanding - base size only`
under a `RECOVERY SIZE ENDED` sent two minutes earlier. Accurate, and it reads
as a second subsystem disagreeing with the first. The standing line now echoes
the transition it follows - `Recovery size ended - $0.16 still outstanding -
base size only` - and the refusal is spelled `no recovery add - <reason>` in
both the recap and the status header, which had two spellings for it.

**Nothing about sizing, the exit rule or the ledger changed.** The 50%-and-
four-wins rule (section 48) fired correctly at 91% and 4 wins; the deficit,
the transition machine and the money were right throughout. This was the
report.


### The cause behind the report: every add that day was refused by a 1s lag

The reason string on that row was not incidental. **Every recovery add on
2026-09-23 was refused for `crossing history unavailable for this window` -
25 of 25** - and not one of them was a decision about a market.

`crossed_since(entry_ms, side)` answers "has BRTI been on the wrong side of
the strike since we bought?" and returns None when it cannot see far enough
back. The add is evaluated on the SAME poll that creates the entry. No broker
fill has synced yet, so `position_entry_ms` falls back to the proposal's own
timestamp - and BRTI's `live_data` timeseries is quantised to whole seconds
and trails real time by a second or two. So there is no sample at or after the
entry instant, the window is empty, and the honest answer is "unknown".

Measured on all 25, newest BRTI sample against the entry timestamp:

    lag 0.0s to 1.9s        25 of 25 had NO sample at or after entry

**The margin is sub-second, in both directions.** The first entry after the
deploy was covered - newest BRTI sample 1.7s AHEAD of the entry instant, and
the crossing answered `False` normally. So this is not a feed that is broken
or permanently behind; it is a check standing on a ±2s boundary, which is
exactly why the fix belongs in how an unknown is HANDLED rather than in the
feed or the gate.

`crossed_since` was right. What was wrong sat above it: **the position gets
ONE evaluation.** `_step` returns on any existing row, so the terminal skip
that an unknown produced was permanent for that market - the question was
never asked again, though it became answerable a second later.

### Unknown is still not a pass. It is no longer a verdict either

The distinction that was missing is between a DECISION and a QUESTION THAT
COULD NOT BE ASKED YET.

* The conservative assumption stands: the add is evaluated as though it DID
  cross, which vetoes, and nothing acts on the other branch. **Unknown never
  places.** The gate was not relaxed, no observation is fabricated, and the
  history requirement is not bypassed.
* A second evaluation decides only whether that veto is TERMINAL. If the
  unanswerable question is the only thing in the way, the row is written
  `RECOVERY ADD DEFERRED` and the next poll asks again. If the add would have
  been refused anyway, that refusal is real, is independent of the crossing,
  and is recorded **under its own reason** - so "recovery is not active" and
  "distance collapsed to 7.7x" stop being mislabelled as a feed problem.
* A deferred row resolves by itself: it places, or a real refusal replaces it,
  or the add deadline passes and `evaluate` refuses on the clock.

`record_or_advance_add` advances a row **only while it is DEFERRED**, in SQL.
Every terminal state still refuses exactly as the bare insert did, so the
one-add-per-position guarantee and the deterministic `client_order_id` are
untouched and no second contract can be opened.

### What it cost, replayed against the rows themselves

The 26 refusals were replayed through the REAL `evaluate` with the REAL
recorded inputs - distance, momentum, side, staleness, remaining seconds,
deficit and fill, read back from `conditions_at_placement` - and the crossing
answered the way the next poll would have answered it:

    passed the replayed decision gates     9
    recovery was not active               16
    economics refused it                   1

So **nine opportunities passed the replayed decision gates**, not twenty-six.

**That is the ceiling, and it is a count of DECISIONS.** A replay establishes
what `evaluate` would have returned on the recorded inputs and nothing else.
It does not establish that Kalshi would have accepted the order, that a bid
resting 2c under the fill would ever have been hit, or that the crossing check
would still have passed on the later poll that actually placed it. The real
number of adds is at most nine and could be zero. An earlier draft of this
section said "nine adds the rule wanted and did not get", which claimed fills
a replay cannot see.

**The other sixteen are the second harm.** Those markets had no deficit at
all - the honest reason was "recovery is not active" - but the old override
replaced whatever `evaluate` had decided, so a routine non-event was filed
under a data fault. Sixteen of the day's twenty-six "crossing history
unavailable" rows were never about crossing history. Reasons are now taken
from the branch that actually refused.

**This changes live behaviour.** `RECOVERY_ADD_ENABLED=true`: adds are real
money, $30 test budget, one contract per position. Adds will now be placed on
markets where the gate was silently vetoing every one of them. That is the
rule working as designed, not a new rule - but it is a real change and the
first cycles should be read as such.

**Still deliberate, and now reachable for the first time:** `_maintain`
cancels a RESTING add when the crossing cannot be verified. That is the safe
direction - a bid whose justification cannot be checked should come off - and
it is left alone. It was almost unreachable while nothing ever rested; a brief
feed gap will now cost an add, which is the correct trade.

### Not introduced here, found while verifying

Seven `RECOVERY ADD EXECUTED` rows have `settled = 0` long after their markets
closed, so `unsettled_filled_adds` never drains. The ledger and the money are
read from `/portfolio/settlements` and are unaffected - this is the add's own
per-leg P&L bookkeeping, which reports rather than decides. Unmeasured, and
untouched here.

### Deployed and watched, 2026-09-23 21:50Z

Revision `59bb6ba`, restarted through the watchdog at 21:50:45Z; the service
prints its own revision on startup, which is how the running code was
confirmed rather than assumed:

    BTC15 signal started; revision 59bb6ba (main, clean)

The log shows the change on the first evaluation after the deploy. Before:

    17:36:54  recovery add SKIPPED [...231745-45]: crossing history unavailable
    17:51:54  recovery add SKIPPED [...231800-00]: recovery is not active

The second market had no deficit. Under the old code that row would have been
filed under a data fault too; the reason now comes from the branch that
actually refused. Its `conditions_at_placement` recorded `crossed: false` -
the question was answerable that time - with the newest BRTI sample 1.7s
AHEAD of the entry instant.

One complete market was watched end to end (21:45-22:00Z): entry filled 2 @
78c, add evaluated and refused honestly, sold early at 99.6c, settled, and the
recap sent to Telegram read

    🔧 No recovery add · recovery is not active

where the old code would have said "Recovery add: pending · rested at 76c".

The following 15-minute window (22:00-22:15Z) was watched complete and traded
nothing - ask 69c against a 70-93c band, BRTI distance 5.5x against 10x - so
the add path was not exercised there. Poll, BRTI recording and settlement sync
stayed fresh throughout (123 BRTI rows, poll ~10s, sync ~60s), with one 110s
observation gap at the window boundary that the log accounts for as "between
windows; waiting for the next market".

**NOT YET OBSERVED LIVE: the deferral itself.** Reaching it needs an active
deficit at the moment an entry fills AND the crossing unanswerable on that
poll. The deficit cleared to $0.00 at 21:49Z, so no add has been evaluated
under an active cycle since the deploy. It is covered by tests, not by
evidence from the live system, and should be confirmed on the next cycle: a
`RECOVERY ADD DEFERRED` row that resolves within a poll or two.

## 52. The add-on's lifecycle: a step that was never written, and an orphan (2026-09-23)

Verifying section 51 turned up four more defects in the same subsystem. Three
of them were unreachable until section 51's fixes made adds actually rest, so
they were shipped-but-dormant rather than new.

### The settlement step did not exist

`recovery_adds.settled` and `.realised_pnl` were declared, and
`unsettled_filled_adds` was written to find the backlog. **Nothing ever called
it, and nothing ever wrote either column.** All 7 filled adds sat at
`settled = 0` with a NULL P&L, so `add_pnl_summary` reported **$0.00** for the
add-on however much it had made. The one number the live test exists to
produce - does the SECOND contract pay? - had never once been computed.

Reconciled against `/portfolio/settlements`, the broker's own record:

    KXBTC15M-26SEP221830-30  UP    1 @ 0.83  fee 0       yes   +0.1700
    KXBTC15M-26SEP221845-45  UP    1 @ 0.72  fee 0       yes   +0.2800
    KXBTC15M-26SEP221915-15  UP    1 @ 0.82  fee 0       yes   +0.1800
    KXBTC15M-26SEP222115-15  UP    1 @ 0.84  fee 0       yes   +0.1600
    KXBTC15M-26SEP222230-30  UP    1 @ 0.75  fee 0.0132  yes   +0.2368
    KXBTC15M-26SEP230545-45  DOWN  1 @ 0.73  fee 0       no    +0.2700
    KXBTC15M-26SEP231615-15  DOWN  1 @ 0.83  fee 0       no    +0.1700
                                                       total  +1.4668

Seven for seven, +$1.4668. Small numbers on one contract each, and far too few
markets to conclude anything about the rule - but it is the first time the
figure has existed at all.

**The evidence is a settlement row, never a clock.** A market that merely
stopped trading has settled nothing, and a result that cannot be read (void,
blank) leaves the add open rather than guessed. **It moves no money:**
`daily_ledger` already holds the broker's P&L for the WHOLE position and the
deficit is already credited from it, so the figure written here is the add
leg's own and is read only by reporting. Checked on a snapshot: the ledger,
the deficit and the add budget are byte-identical before and after, and a
second run closes nothing.

One attribution, stated rather than assumed: an early exit is credited to the
BASE first, so only an exit LARGER than the base reaches the add. On all seven
the exit exactly equalled the base count, so the convention does not bite on
any real row.

### `NameError` on every successful fill

    print(f"... fee {fee:.4f}, {'maker' if maker else 'taker'}")

`maker` was never bound in `_bank_if_filled` - the parser returns `is_taker`.
Every successful fill raised, AFTER the fill had been banked and the funds
released, so the row was right while the caller saw an exception: the EXECUTED
line was never logged and the rest of the poll was abandoned. It was
unreachable only because `order_status` hit a route that 404s for every order,
so `parse_fill` always returned None and the line was never executed. Fixing
that route (section 50) armed it; adds that actually rest make it certain.

Tests passed throughout because they call through `step`, which swallows it,
and the DB write precedes the print. The regression asserts the RETURN VALUE.

### The ambiguous submission, and the orphan it made

The local row is written before the order is sent. A crash - or a dropped
response - between the send and storing the broker's `order_id` leaves a row
saying PENDING with `placed_ms` set and `order_id` NULL. Every repair path was
keyed on `order_id`:

* `adds_needing_reconciliation` filtered `order_id IS NOT NULL` - excluding
  precisely the row that needed resolving;
* `_maintain`'s fill check short-circuited on a falsy `order_id`, so the order
  was never polled for a fill;
* `_cancel` marked the row CANCELLED **without sending anything to Kalshi**.

So an order that was really resting became an orphan: never watched, never
cancelled, and if it filled, never banked, never charged to the budget and
never in the add's P&L. The docstrings promised "`reconcile` resolves it
against the broker"; for this row it could not.

`client_order_id` is a pure function of (ticker, side, window), so it is the
one key that survives the crash, and Kalshi returns it on the order object.
`order_by_client_id` lists the ticker's orders and matches on it, which is the
way back to the `order_id`. **A listing that could not be READ raises rather
than returning "not found"**, because concluding "no such order" from a failed
read is how a live order gets written off.

### "I could not reach Kalshi" was recorded as "the order is gone"

Two places collapsed an unreadable answer into a definite one:

* `_cancel` wrote CANCELLED unconditionally after a failed cancel - a 500, a
  429 or a timeout all landed there - leaving the order live at Kalshi under a
  row saying it was gone. A CANCELLED row is never examined again. Only a 404,
  which means the exchange has no such order, is proof; everything else now
  leaves the row PENDING to retry.
* `reconcile` folded `status is None` into the cancelled list, so one failed
  GET at startup marked a live resting order CANCELLED, permanently.

**The conservative cancellation rule is unchanged and is meant to stay:** an
unverifiable crossing still pulls a resting order. What changed is only that
an unverifiable ANSWER is no longer recorded as a verified one.

### A deferred row could be stranded non-terminal

DEFERRED (section 51) is re-entered only while the same window is open, the
base position is still held and BRTI is available. `main.run` does not even
call `step` once `open_position_detail` returns None, and `open_add` is keyed
on the current window - so an exit, an input gap or a window roll left the row
open forever, counted as undecided in the add-on's own statistics.
`close_stale_deferred_adds` closes it on either definite condition - the
window has closed, or the base position is gone - and can place nothing,
because a deferred row never sent anything.

### Still open, measured and not fixed

* **A partial fill terminates the row while the remainder rests.**
  `record_add_fill` writes EXECUTED for any `count > 0`, and `_step` then
  returns on every later poll, so an unfilled remainder is never maintained
  and never cancelled. The add is one contract, so a partial needs a
  fractional fill, which Kalshi's `_fp` fields permit but which has not been
  observed here. Named, untested, unfixed.
* **`open_mark` is up to ~60s stale** where it feeds the add's exposure gate,
  because it is written by the 60s settlement sync. It can under-count
  exposure just after a fill and over-count just after an exit. Left alone:
  changing it would move a trading gate, which is the operator's call.
* **Transient BRTI conditions are terminal for a RESTING order.** The place
  path gained DEFERRED for a briefly-behind feed; the maintain path has no
  analogue, so a one-poll gap permanently ends the add. That direction is
  safe - it cancels rather than buys - and it is left as designed.

## 53. Crossing-history coverage, fixed at the source (2026-09-23)

Sections 51 and 52 stopped a coverage miss from becoming a permanent refusal.
This one fixes the coverage.

### What the gate could and could not establish

`crossed_since` returned a bare `None` from four different situations, so the
caller printed one sentence over all of them. "The feed is 1.4s behind" and
"there is no series at all" were the same message, and the first resolves
itself on the next poll while the second does not.

It now returns a `Crossing` carrying the verdict, the reason it could not be
reached, and how far short the series falls. Coverage must hold at BOTH ends:

| condition | meaning |
|---|---|
| series starts after the entry | an earlier crossing would be invisible |
| series does not reach the entry | no sample in the interval at all - the routine one |
| series is stale | **new**, see below |
| clock and series disagree by an hour | a seconds-for-milliseconds slip |

**The staleness rule makes the gate STRICTER, not looser.** A series that
covers the entry but stopped 60 seconds ago used to answer a confident
`False` - no crossing - for an interval it had stopped watching. That was a
false negative in the unsafe direction, and it is now unknown.

**The units rule exists because the failure is silent.** Passing seconds where
milliseconds are expected makes `now - last` hugely negative, so the staleness
check simply never fires and a series of any age answers confidently. It
raises nothing, so it is caught by magnitude.

### The entry instant is confirmed from the broker

`fills` was written only by the 60-second ledger sweep, so for up to a minute
after an entry the gate measured from the proposal's `created_at` - when we
ASKED, not when we were filled. `fills_for(ticker)` confirms it on the add
path, once per window, and only while the broker's own fill is still missing.
This makes the measurement CORRECT; it does not make coverage easier, because
the true fill is later than the proposal and so needs a later sample.

### Bounded retries, and a termination that says what happened

Deferral repeats only while the add could still be placed - `evaluate` refuses
below `min_seconds_remaining`, so the eligibility period is the bound and no
counter is needed. When that period ends while coverage is still short, the
recorded reason names **both**: the deadline, and what we were still waiting
for. Previously it named only the clock, which hid the cause.

### What was not done, deliberately

* `None` is never replaced with an assumed `True` or `False`.
* No current price stands in for history.
* No threshold, sizing limit or safeguard was changed.
* The conservative cancellation of a RESTING add on an unverifiable crossing
  is untouched.

There is no authoritative source that can supply a BRTI value for an instant
BRTI has not published yet. The honest handling of that second is to wait for
it inside the eligibility period and say so, which is what this does.

### Validation

1,114 tests pass (+25). The new ones cover feed lag, the timestamp boundary in
both directions (a sample exactly at the entry answers; one millisecond past
the last sample does not), restart with a short buffer, missing samples, a
mid-series gap, staleness, the units slip, and exhausted eligibility - plus
the 25 recorded lag values measured from the live database, each asserted to
be a sub-two-second miss that answers once the feed catches up. Duplicate-order
protection and fill/cancel reconciliation were re-run unchanged.

Deployed as `24b4dbe` at 19:33:28Z. Startup: single instance, no exceptions,
observations 7.9s fresh, settlement sync 31.7s, BRTI 4.7s, and nothing left
unreconciled - 0 non-terminal adds, 0 unsettled filled adds, 0 rows PENDING
without an order id. The newest BRTI row at that moment carried `ts_ms`
725ms behind `received_ms`, which is the lag this section is about, visible
live and well inside what the gate now waits for.

## 54. The live intelligence: what it does, and two numbers it fed itself (2026-09-23)

Asked to verify the intelligence and self-learning are running. They are. Two
of the numbers they feed on were wrong, both in the same way - always present,
so every surface looked healthy, and always meaningless.

### What is actually live

| | |
|---|---|
| active policy | `kalshi-brti-2-1790189147`, 29 arms, valid on load |
| decisions | 582 under this policy, ~100/hour, latest within a minute |
| feature health | `features_ok = 0` on **none** of them |
| diversity | 30 context keys, 24 evidence levels, 16 distinct reasons |
| authority | `None` on all 582; vetoes off, admissions off, `overrides_gate` never set |
| its one effect | the confidence delta → the header word |
| learning loop | 5 runs, all `ok`, 0 consecutive failures, next fit on schedule |

**The confidence adjustment is real and reaches the operator.** Replaying the
recorded model score against the recorded delta through `confidence_label`:
**31 of 581 decisions actually changed the header word** (e.g. 83 points +8 →
MEDIUM becomes HIGH), with 107 more applying a delta that did not cross a
label boundary. This is not the FINDINGS 49 state, where 928 of 1,097
decisions were the identical refusal.

**Whether the adjustment is any GOOD is not yet answerable**, and what little
there is points the wrong way: over 45 graded markets, raised-confidence won
5/6 (83%), lowered-confidence won 9/9 (100%), unchanged 27/30 (90%). Markets
it was least sure about won most often. At n=6 and n=9 that is noise, not a
refutation - but it is not evidence the calibration works either, and it
should not be described as working until the counts are an order of magnitude
larger.

### Defect 1: the graded P&L was a literal zero

`main` called `grade_intelligence(window, winning_side, 0.0, now_ms)` with a
constant. All 557 graded rows under the live policy carried `realised_pnl =
0.0` - a column always present, always zero, never true. `learning_data`
already had to route around it and recompute from broker fills, with a comment
saying so; anything else reading it scored every trade as break-even.

Each row is now graded at **its own decision-time ask**, the same per-contract
counterfactual `grade_candidates` uses one method above: `(1 if won else 0) -
ask - fee`. A row with no recorded ask grades **NULL, not zero** - "unknown"
and "break-even" are different claims. The account's own money is untouched
and still comes from `daily_ledger`.

### Defect 2: retired-feature evidence was gating live arms

`forward_scoreboard` pooled **every** candidate evaluation ever written, with
no filter on feature version, and that number gates both promotion
(`forward evidence contradicts it`) and withdrawal (`deteriorated`).

A context key is only a label. `asia · mid · bd10-15 · px85-94` computed from
brti-1 features is not the same population as the identical string computed
from brti-2. On this database 7 of 16 forward rows are `brti-cand-1790130402`
- fitted before the feature reset - and they were being mixed into the
evidence used to judge brti-2 arms. That is the FINDINGS 49 mistake again: a
retired artefact still answering.

The runner now passes the running contract. The stale context drops out:

    pooled    asia · mid · bd10-15 · px85-94   7 changes  -0.8542
              bd10-15 · px85-93 · mom5+        7 changes  +1.1645
              bd15+ · px70-85 · mom5+          2 changes  +0.6607
    brti-2    bd10-15 · px85-93 · mom5+        7 changes  +1.1645
              bd15+ · px70-85 · mom5+          2 changes  +0.6607

### Related, measured, and NOT changed

The operator asked whether a candidate that vetoes only winners should be
invalidated. `c01` did exactly that - 7 forward changes, 7 winners vetoed, 0
losers, **-0.8542** - and nothing marked it invalid.

It is not reachable: `deteriorated()` only withdraws **promoted execution
arms**, and the promotion gate only consults forward evidence once
`changes >= MIN_WITHDRAWAL_N` (20). At 7 changes a bad record gets no vote.
Nothing has ever been promoted, so nothing has been at risk.

Lowering that threshold is a statistical judgement about how few forward
markets may overturn a validated arm, and it is the operator's to make, not a
correctness fix - acting on 7 observations is the same overfitting the
Holm-Bonferroni correction exists to prevent. **What WAS a correctness fix is
that c01's rows should never have been in the brti-2 pool at all**, and after
defect 2 they are not.

### Still true, and worth restating

`filled` and `order_id` are NULL on every `intelligence_decisions` row. The
training path reconciles executions from broker fills instead, so the loop is
not blind - but the columns are unpopulated and a reader joining on them gets
nothing. Unmeasured, untouched here.
