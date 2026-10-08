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

## 55. Intelligence in the execution loop, and session-aware evidence (2026-09-23)

The operator corrected the standing instruction: the layer is intended to
operate in the live execution loop, and shadow was the wrong setting.

### The wiring already existed and was correct

`intelligence_verdict` runs BEFORE the auto trading block, and an authorised
veto closes it:

    if intel_verdict.final_action == intel.VETO:
        rule_match = False           # main.py, line ~2033
    ...
    if rule.enabled and rule_match and auto_on and trader is not None:
        ...                          # the trading block, line ~2236

`rule_match` is the flag the block is gated on, so a veto means no order is
submitted. Nothing needed building; what was missing was the authority, and
one thing the evidence could not see.

Two switches, both required, neither raised by any code path:
`INTELLIGENCE_MODE=live` and `INTELLIGENCE_AUTHORISED=true`. `may_veto` and
`may_admit` are separate permissions from `may_confidence`, and
`may_change_size` returns False for **every** mode - sizing remains the
operator's alone.

### The evidence could be spent in a session that never earned it

The cell key is `distance · price · momentum`. Session is deliberately NOT in
it - keying on it fragments cells below the point where they can speak, which
is why `brti-2` removed it. But a pooled cell can be carried entirely by
sessions other than the one being traded, and on 2026-09-23 it was:

    bd10-15 · px70-85 · mom5+        n=431, "pattern supports the decision"
        us        3W-1L
        late-us   3W-0L
        asia      0W-2L              <- two UP entries, back to back, -$1.60 each

Both were rated 89 points -> **HIGH**, 10.06x and 10.75x distance, momentum
+7.47 and +8.21 bps. Every gate passed and the layer endorsed them. The
support was real and it was someone else's.

**The key stays pooled** - `n`, `MIN_VALIDATE_N`, the Holm-Bonferroni
correction and the promotion bar are all untouched, and no requirement was
lowered. What changed is that arms now carry `by_session` counts BESIDE the
evidence, and an execution action on a POOLED key requires that the session
being traded holds at least `MIN_SESSION_EVIDENCE` (30) of the cell. It can
only ever withhold an action; it never creates one.

A key that already names its session (`us · mid · bd10-15 · px70-85`, the
brti-1 shape) is session-pure by construction and needs no such check.

### The documented fallbacks

| situation | behaviour |
|---|---|
| arm predates `by_session` on a pooled key | execution withheld; confidence still applies |
| this session below the floor in that cell | withheld, reason names the session and both counts |
| caller supplies no session | old contract; the live path always supplies it, pinned by a test |
| fingerprint mismatch / retired family / inactive policy / thin n | base strategy decides, unchanged |

### Where this leaves it

Authority is ON and connected. The active policy `kalshi-brti-2-1790210737`
has 29 arms, **0 promoted**, `vetoes_enabled=False`, `admissions_enabled=False`
and no arm carrying `by_session`. **No policy is currently eligible to affect
a trade.** Nothing was promoted to make activation claimable, and that is the
correct state: the bar was not met.

The next refit writes `by_session` into every arm. Until an arm clears
validation, the multiplicity correction AND the session floor, the layer is
connected and authorised with nothing actionable to say - which is not trade
protection and must not be described as such.

### 55b. The decision-to-order link (2026-09-23)

`intelligence_decisions.order_id` and `.filled` were declared and never
written - NULL on all 2,635 rows. Nothing broke, because `learning_data`
reconciles executions from broker fills instead, but the columns were dead: a
query joining a decision to the order it caused returned nothing, and every
executed trade looked simulated.

`link_intelligence_orders` joins them per WINDOW, which is the granularity
that is true. A decision row is written every poll and only one poll produced
the order, so `filled` means "this market was traded", not "this poll placed
it" - the second is not a fact any single row can carry. An untraded market
gets `filled = 0`, not NULL: "we did not trade this" is worth recording.

Run over the archive: 2,640 rows linked, second run 0. Of the 106 windows
carrying decisions, 60 were traded - and `trade_proposals` holds exactly 60
filled windows inside that date range, so the link is complete rather than
merely plausible. A `pending` proposal is an intention and does not count.

Unrelated, resolved while checking: the refit was NOT overdue. The learner's
own `settled_markets()` reads 97 against a watermark of 87 - **+10**, below
the +24 trigger - where a naive `COUNT(DISTINCT ticker) FROM daily_ledger`
suggested +97. The ledger counts every settled market; the learner counts what
its own corpus can use. Use the learner's number when asking whether a fit is
due.

## 56. Two hours with no market, and nobody told (2026-09-24)

The operator: *"The telegram stopped two hours ago."*

### What actually happened

Kalshi listed **no 15-minute market between 07:00Z and 09:00Z**. Observations
per hour, from the archive:

    06:07-07:07Z   258   4 markets     normal
    07:07-08:07Z     0   0 markets     <- nothing listed
    08:07-09:07Z    37   1 market      recovering

The service was healthy throughout - the poll loop turned, the reference
recorder kept writing, settlements reconciled, the log was written 0.9 minutes
before the check. It did exactly the right thing and said so **once**:

    03:00:03  between windows; waiting for the next market
    05:00:28  Live market data connected: KXBTC15M-26SEP240515-15

and then nothing for two hours. A direct query confirmed the exchange had a
live market again by the time it was investigated, with quotes - so this was
an upstream listing gap, not a discovery bug.

**The defect is that it was silent.** A window boundary is SECONDS; two hours
is an outage. The operator's only symptom was that Telegram had gone quiet,
and the absence of alerts is not an alert - it is indistinguishable from a
dead bot, a broken token or a crashed service. This is the silent stop this
system is most exposed to, and it went unreported for its whole duration.

Now: past `market_gap_alert_s` (600s, far above any real boundary) the gap is
reported once, keyed on when it started, and an all-clear follows when a
market returns. The alert says what is NOT wrong as prominently as what is,
because "no market" read on a phone at 3am looks exactly like a dead bot, and
it states that an open position is unaffected.

### The linker wrote a false zero, within hours of shipping

`link_intelligence_orders` (55b) ran on every 60-second settlement sync,
including for windows whose order had not filled YET. It wrote `filled = 0`,
and then never revisited, because it only considered rows where `filled IS
NULL`. Two windows carried a false zero against orders that really traded
(`01a0d189-...` exited, `01a0d1dd-...` filled).

It now links only windows whose decisions are GRADED - settlement has
happened, so the answer can no longer change - and self-heals any window whose
rows say `filled = 0` while a filled order exists, which repaired both without
a migration. Verified: 2 wrong before, 24 relinked, 0 wrong after, second run
0.

That was mine, shipped the same evening, and found by reading the archive
rather than by anything failing.

## 57. What a bad day is, and why no recovery rule earned its place (2026-09-24)

The operator asked for a better recovery system, backtested against what a bad
day actually looks like. The measurements refuse the request, and the reason
is worth more than the rule would have been.

Reproduce with `scripts/measure_ask_margin.py`,
`backtest_recovery_variants.py`, `measure_band_walk_forward.py` and
`measure_recovery_paired.py`.

### A bad day is not a low win rate

The live record, by New York day:

    09-19   7W- 9L (44%)   -1.03
    09-20   9W-11L (45%)   -4.15
    09-21  38W- 8L (83%)   +1.90
    09-22  47W- 9L (84%)   +4.47
    09-23  44W- 9L (83%)   -1.42   <- same win rate, lost money

**09-23 won at 83% and still lost.** Because the break-even win rate is not a
constant - it is set by the price paid:

    day      win rate   break-even   margin
    09-21      82.6%       78.9%      +3.7%
    09-22      83.9%       77.1%      +6.9%
    09-23      83.0%       84.4%      -1.4%

The average loss on 09-23 was -1.65 against -0.87 and -0.90 on the two
profitable days, while the average win rose only from +0.27 to +0.31.

**Over the whole live record the margin is -0.07%**: break-even 76.0%, actual
75.9%, payoff 3.16:1 against. The strategy runs on a knife edge of one to
three points of win rate, and a "bad day" is any day the price it paid moved
the bar above where the win rate landed.

Two things this is NOT: wins are not being cut short (early exits capture
**98%** of full-hold potential), and size is not the culprit (1-contract
margin +1.1%, 2-contract +2.5% - the 2-contract trades simply ran at a higher
average ask, 82c against 78c).

### No recovery variant beat doing nothing, with evidence

3,049 corpus markets in the deployed band, 68 days, one row per market at
`remaining = 10`:

    variant                        taken    total  per trade   max DD  worst day
    A baseline: flat 1              3049   +21.92    +0.0072   -18.19     -5.55
    B deployed: deficit upsize      3049   +14.70    +0.0048   -19.67     -5.55
    C control: flat 2               3049   +43.98    +0.0144   -36.35    -11.10
    D selective band after a loss    630   +20.43    +0.0324    -5.22     -1.71
    E stand down after 2 losses/day  750    +5.90    +0.0079    -7.86     -1.54
    G selective + flat 2             630   +40.90    +0.0649   -10.44     -3.43

**C reproduces FINDINGS 32 exactly** - x2.01 profit against x2.00 drawdown -
which validates the harness and restates the thing that keeps being true:
size creates no edge, it scales both sides.

**D and G looked like the answer and are overfit.** They select the
0.85-0.90 aligned band, chosen by looking at this same corpus. Choosing the
band on the FIRST half and scoring it on the second:

    band chosen on train only : 0.75-0.80 aligned  (train edge +0.0690)
    its out-of-sample result  : -0.0364 over 361 markets, CI SPANS ZERO
    baseline on the same half : -0.0009

The rankings reshuffle completely between halves and **not one band's
out-of-sample interval clears zero**. The in-sample story - same profit at a
third of the drawdown - was noise dressed as a rule.

**B, the deployed rule, is the only fair comparison** - a fixed rule, same
markets, so it can be paired. Day-clustered bootstrap over 68 days:

    baseline +21.92   deployed +14.70   difference -7.21
    95% CI [-18.35, +3.57]  -> SPANS ZERO
    upsized on 156 of 3049 trades (5.1%); worse on 12 of 68 days
    worst day identical under both (-5.55)

So the deployed upsize **cannot be shown to help, and cannot be shown to
harm**. It fires rarely and does not worsen the tail. Its point estimate is
negative, and there is a mechanism for that - `2*(1-ask) >= deficit/4` upsizes
preferentially into CHEAP asks, and the cheap band carries the weakest margin
(+0.7% at 0.70-0.75) - but the evidence does not establish it. Left alone.

### Why the request cannot be granted

**The corpus baseline edge is not stable out of sample**: +0.0072/contract on
the whole, **-0.0009 on the test half**. Recovery rules protect or amplify an
edge; there is no stable edge here for one to act on, so every recovery
variant is arithmetic applied to noise.

The binding constraint is the ENTRY, not the recovery. A bad day is made by
the break-even arithmetic - the price paid against the win rate achieved - and
the only lever with a real mechanism behind it is paying less, not sizing more
or recovering faster.

**Nothing was changed.** The deployed recovery stays exactly as it is.

## 58. Two indicators the gates could not see (2026-09-24)

The operator brought two charts. Both show a market the four deployed gates
call clean and the outcome calls a coin toss.

### The reversal — SHIPPED AS A BLOCKING GATE, on the operator's decision

06:08. Target 83,234. BRTI ran to ~83,340, comfortably above; the position was
taken UP at 51c; BRTI collapsed to 83,191. The trade lost $0.62 with every
check green:

    Confidence HIGH · Entry checks 4/4
    Price 92c · Distance 20.3x · Momentum +41.6 bps · Reference fresh
    Band held 187s

None of them can see it. Momentum reads POSITIVE because the five-minute
window still contains the run-up. Distance is large because BRTI is far from
the strike - on its way back through it. The gates describe where the price
IS; none asks whether the move that put it there is still alive.

`brti_retrace` is how much of the recent advance has been handed back, in the
direction the setup is taken. 0.0 at the high-water mark, 1.0 when the whole
move is gone.

**THE EVIDENCE, stated beside the decision.** 79 markets with a Kalshi BRTI
path and a settled outcome, entry at 600s:

    retrace band        n    W   L   win rate        95% CI
    0-25% (stable)     55   47   8     85.5%   [73.8%, 92.4%]
    25-50%              6    4   2     66.7%   [30.0%, 90.3%]
    50-75%              7    5   2     71.4%   [35.9%, 91.8%]
    75%+ (reversed)     8    6   2     75.0%   [40.9%, 92.9%]

    as a gate at 0.60: kept 83.9% vs refused 70.6%, separation +13.3%

**And the caveats, equally plainly.** Every band's interval overlaps every
other. The cells are n=6, 7, 8. At a 180-second window the separation
collapses to +2.5% and goes NEGATIVE at two thresholds - same data, one
parameter changed, which is the signature of a fitted choice. This session
has already watched three in-sample winners fail out of sample.

The recommendation was to record it first and gate later. **The operator chose
to gate now**, and that is their call: the mechanism is sound, the gate only
ever refuses, and the downside is bounded. The threshold is chosen from 79
markets and should be revisited when there are hundreds.

Unmeasurable is NOT a pass: a setup whose recent path cannot be computed fails
the gate, because "looks fine" and "was checked" are different states.

### Choppiness — CONFIDENCE ONLY, by instruction

06:45. Target 83,232.53. Over the hour BRTI crossed the strike six times,
printed four peaks, and finished $11.74 away. That market is a coin toss
however far the last print sits from the target.

`brti_choppiness` is net movement over distance travelled: 0.0 a straight
line, 1.0 thrashing that ends where it began. Measured over the 15-minute
window rather than the reversal's two minutes - thrashing is a property of the
session. On synthetic paths: clean trend 0.000, trend with noise 0.000, pure
oscillation 0.957.

**It is not a gate and the tests exist to keep it that way.** The operator was
explicit that choppiness influences confidence only, so it appears in no
`check_facts` list, `KalshiBRTIRule` carries no threshold for it, and the
suite asserts that the choppiest possible window still qualifies. It enters
`confidence_label` as points on the same 0-100 scale as the learned
calibration, proportional rather than stepped, and can only ever LOWER the
word - the same authority a confidence adjustment has always had.

### What adding a fifth gate did to the confidence scale

`base_points` is a five-entry table clamping at four agreeing checks, so
`agreeing=5` scores 100 exactly as `agreeing=4` did. A TRADED setup therefore
scores identically on the live path and in the training corpus, which is what
the calibration compares. The corpus cannot reconstruct the retrace at all - a
corpus row carries no intra-window path - and `agreeing = 4 - failures` is
left unchanged deliberately for that reason. A setup failing only the reversal
gate is never traded and is recorded with `rule_match = 0` regardless.

1,213 tests pass.

## 59. The hedge, optimized properly, and the reason it cannot work (2026-09-24)

The operator asked for a hedge: alongside the main entry, buy the cheap
opposite side and sell it early for whatever profit it offers while the main
runs. Then, after three rounds of fixed configurations, the instruction that
mattered — **"Do not just stay on fixed numbers. Always try to find the sweet
spot."** That was right, and the sweep that followed is the answer.

### What was searched

12,384 cells over the real 10-second book, both sides quoted. The hedge is
bought at its ask and sold at its bid — the prices a taker gets, not the mid.
Kalshi only.

| parameter | range |
|---|---|
| entry | 780s, 720s, 660s, 600s, 540s |
| max hedge cost | 0.100 to 0.400, in 0.025 steps |
| profit target | −0.05 to +0.30, in 0.01 steps |
| deadline | expire, or sell regardless at 60/120/180/240/300/360/420s left |

The deadline is there because the operator's rule has two parts that the
earlier tests had collapsed into one. "Sold earlier regardless" does not mean
*sell when green*; it means a losing hedge is closed for what it is still
worth instead of being left to die at zero. Those are separate parameters and
they were swept separately.

**218 of 12,384 cells were positive — 2%.** Median per-hedge across the family
−0.0445.

### The maximum was an artefact, and finding that out is the point

The best cell was entry 780s, cost ≤ 0.30, sell at +0.30, else expire:
+2.67 over 30 hedges. An earlier, cruder sweep had put "hold to settlement"
on top of the same subset.

Neither is a hedging result. One YES plus one NO always pays exactly $1.00 and
always costs 1.010 plus two fees, so a *held* hedge is profitable precisely
when the main leg loses. At 780s the main wins 52.8% while paying 74.5¢ and
loses **−0.23 per market**. The optimizer found a bad entry time, not an edge,
and dressed it as a hedge. Holding both legs there is a guaranteed −1.09.

On its own terms the maximum does not survive either:

```
day-clustered bootstrap 95% CI on per-hedge : [-0.2579, +0.2628]   includes zero
walk-forward, chosen on 09-20..09-22, scored on 09-23..09-24:
  train  n=16  +1.11  (+0.0696/hedge)
  TEST   n=17  -1.12  (-0.0662/hedge)   FAILED OUT OF SAMPLE
```

That is the fourth in-sample winner this session to reverse out of sample.

### Why no sweet spot exists

The hedge is priced for exactly the event it pays on.

| entry | hedge ask | +fee | main actually loses | edge if held |
|---|---|---|---|---|
| 780s | 0.265 | 0.279 | 47.2% | +0.1934 |
| 720s | 0.239 | 0.252 | 29.8% | +0.0467 |
| 660s | 0.220 | 0.232 | 27.4% | +0.0420 |
| **600s** | 0.215 | 0.227 | **20.0%** | **−0.0269** |
| **540s** | 0.203 | 0.214 | **19.0%** | **−0.0246** |

At the times the system actually enters, the opposite side costs about what it
is worth and slightly more. The positive rows are the ones where the main leg
is losing, which is a statement about entry timing, not about hedging.

So the hedge can only earn from the *option to sell early* — and that option
is adversely selected. It goes deep green when BRTI has moved against the main
position, which is the same thing as the main being about to lose. Across
12,384 exit rules, the best target at every single entry time was +0.30 or
more: the rule that only fires when the main is already in trouble. Selling on
a small wiggle, the operator's stated rule, was measured at its own best
setting and lost:

```
12 min, 15-25c, sell on any profit  (best of the 36 fixed configurations)
  sold in profit : 19 trades, avg +0.0548, total +1.04
  never went green:  7 trades, avg -0.1963, total -1.37
  NET                                      -0.33
one dead hedge needs 3.6 sold hedges to pay for it
```

The operator's observation that both legs often close green is correct —
13 of 26 at the 12-minute entry. The asymmetry is what defeats it: roughly
5¢ won against roughly 20¢ lost.

### The one thing worth keeping

The main-leg column is not part of the hedge question but it is the strongest
signal in the table: price-band-only selection at 780s / 720s / 660s loses
−8.29 / −4.64 / −6.29, against −0.62 / −0.76 at 600s / 540s. That is a
band-only filter, **not** the live gates, so it is not evidence that the
deployed strategy should move its entry — but it is the third measurement this
session pointing the same way, and it belongs on the list to test properly.

**Nothing shipped.** The hedge is not deployed and no live code changed.

## 60. Buy cheap, sell at 70-90c: the target is the winning outcome, sold for less (2026-09-24)

Operator specification: buy a contract at 20-30c and sell it at 70-90% for
profit. One leg only, no hedge, no second side. Compare against the deployed
strategy.

This is section 33's idea at a **much higher target** - 3-4x rather than 1.5x -
so it was measured on its own terms, on the real 10-second book, 162 markets
over 5 days, taker prices both ways.

### Every configuration falls short, by about half

At any instant one side of a binary is the cheap one. The test takes whichever
side's ask is in the buy band, the first time it is, and sells at the bid.

| buy band | sell at | trades | reached the target | needed to break even |
|---|---|---:|---:|---:|
| 0.15-0.25 | 0.70 | 162 | **18.5%** | 33.9% |
| 0.15-0.25 | 0.80 | 162 | **14.8%** | 29.4% |
| 0.15-0.25 | 0.90 | 162 | **13.0%** | 26.0% |
| 0.20-0.30 | 0.70 | 159 | **22.6%** | 41.1% |
| 0.25-0.35 | 0.70 | 158 | **27.8%** | 48.3% |

All 27 configurations short, across every entry window tried (any time,
5-15 min, last 5 min). Fees and spread do not explain a gap of that size.

### Why: at 70-90c the "exit" is not an exit

| buy band | sell at | won | reached | reached but LOST | won WITHOUT reaching |
|---|---|---:|---:|---:|---:|
| 0.15-0.25 | 0.70 | 16.7% | 18.5% | 6 | 3 |
| 0.15-0.25 | 0.80 | 16.7% | 14.8% | 1 | 4 |
| 0.15-0.25 | 0.90 | 16.7% | 13.0% | 0 | 6 |

Reaching the sell price and winning are very nearly the same event - a handful
of markets separate them out of 162. A binary's price converges to 0 or 1 as
the window runs out, so a cheap side only reaches 70-90c by actually being
about to win. **The take-profit is not collecting a swing while the outcome is
still open; it is selling the winner for 70-90c instead of $1.00.**

Which is why holding beats selling at every single target:

```
buy 0.15-0.25   sell 0.70  -11.62    hold  -10.60    selling costs  -1.02
                sell 0.80  -12.56    hold  -10.60    selling costs  -1.96
                sell 0.90  -11.71    hold  -10.60    selling costs  -1.12
buy 0.25-0.35   sell 0.70  -16.19    hold  -12.27    selling costs  -3.92
```

Raising the target from 1.5x to 3-4x gives up less per winner than section 33
measured, and it is still negative, because the entry is what loses.

### The number

```
BEST OF 27: buy 0.15-0.25, sell 0.70
  162 trades, 5 days, total -11.62, per trade -0.0717
  winners 33/162 = 20.4%   avg win +0.553   avg loss -0.231
  day-clustered bootstrap 95% CI: [-0.0791, -0.0503]   ENTIRELY BELOW ZERO
  losing on 4 of 5 days
```

### Against the deployed strategy, read from the broker

`settlements`, mirrored from Kalshi's own /portfolio/settlements:

| | markets | contracts | total | per contract |
|---|---:|---:|---:|---:|
| **OTM, best of 27** | 162 | 162 | **−11.62** | **−0.0717** |
| deployed, since 09-21 | 125 | 285 | **+5.50** | **+0.0193** |
| deployed, all time | 197 | 938 | +0.17 | +0.0002 |

The deployed strategy is **not** proven profitable - five days of live trading
and its interval still spans zero, and the all-time figure includes two early
days that lost 5.33 on a different configuration. What separates them is that
OTM's interval does **not** span zero. It is the one of the two that is
measurably losing.

### This is the third independent test of the same idea

| test | n | per trade | 95% CI |
|---|---:|---:|---:|
| `reversion_strategy.json` (spike setup, exit 0.50) | 760 | −0.0440 | [−0.0609, −0.0277] |
| section 33 (no setup, exit 1.5x) | 4,876 | −0.0402 | [−0.0488, −0.0310] |
| this one (no setup, exit 0.70-0.90) | 162 | −0.0717 | [−0.0791, −0.0503] |

Three differently-built tests, three exit rules from 0.50 to 0.90, all
negative with intervals clear of zero. The 4,876-trade test is much the
strongest evidence and this small sample agrees with it.

**The cause is structural, not parametric.** Section 1's favourite-longshot
bias: favourites win MORE than their price implies, so longshots win LESS.
Buying the 20-30c side is taking the wrong end of the only durable bias this
market has, and the deployed strategy is profitable to the extent it takes the
right end of that same bias. They are not two strategies to choose between -
they are opposite sides of one bet, and this one is the losing side.

**Decision: not deployed.** Nothing shipped, no live code changed. What would
change it, unchanged from section 33: an entry filter that predicts WHICH
cheap contracts come back, validated walk-forward.

## 61. $3 after a loss: the gain is the ordering, and the risk is hidden (2026-09-24)

Operator specification: on the recorded lifecycles, every time a trade loses,
size the next trade at exactly $3; reset to base after a win. Consecutive
losses stay at $3, so it is a step, not a martingale.

### Two wrong datasets before the right one

Worth recording, because both looked plausible and both were wrong.

**`settlements` yes_count/no_count.** Kalshi books the SALE of a YES as taking
the NO side, so a position opened at 0.82 and closed at 0.997 appears as
"2 yes + 2 no" with `revenue_cents` = 0. 68 of 197 rows are like this. Reading
an entry price out of that gave a **0.639 average entry and a 48.2% win rate**
for a strategy that enters at 0.70-0.93 and wins 76% of its markets.

**`fills`.** `fills.count` carries values like 186.97, 23.3 and 0.17 - not
contract counts. 41 of 188 "buy" fills price below $0.10. A $1 base budget
came out as 10,928 contracts over 139 markets.

**What worked:** `trade_proposals` (integer counts, real fill prices, explicit
`exit_price`) joined to Kalshi's settlement records, **keeping only rows whose
arithmetic lands within 3c of the broker's P&L**. 146 of 154 filled proposals
reconcile. The 8 that fail all show the broker AHEAD of the proposal row -
the recovery add-on's extra contract, which sits outside the proposal.

```
146 reconciled lifecycles, 4 days
  win rate 82.9%   average entry 0.821
  as traded: +0.7074 over 233 contracts (+0.0030/contract)
```

### The result looks good

| rule | contracts | P&L | per contract | max DD | worst day |
|---|---:|---:|---:|---:|---:|
| flat $1 | 146 | −0.26 | −0.0018 | −4.58 | −3.56 |
| $2 after a loss | 171 | +1.92 | +0.0112 | −4.61 | −3.55 |
| **$3 after a loss** | **202** | **+4.70** | **+0.0233** | **−4.37** | **−3.26** |
| $4 after a loss | 230 | +6.45 | +0.0281 | −5.27 | −4.04 |
| $5 after a loss | 260 | +8.85 | +0.0340 | −5.80 | −3.59 |

**+4.96 over flat sizing, and the drawdown got no worse.** Monotone in the
upsize, which is exactly what a real effect looks like.

### It is the ordering

Every dollar of that comes from one fact: the 25 trades that happened to
follow a loss won **92.0%**, against 82.9% overall.

```
after a LOSS   n= 25  win 92.0%   P&L/ct +0.0827
after a WIN    n=120  win 81.7%   P&L/ct -0.0082
permutation p = 0.249  -> INDISTINGUISHABLE FROM CHANCE
```

The decisive test keeps every trade exactly as it was - same entry, same
outcome, same per-contract result - and shuffles only the ORDER. That breaks
the link between a loss and the next trade while leaving the edge, the prices
and the win rate untouched.

```
actual gain of the $3 rule            : +4.96
20,000 shuffles of the same trades    : mean +0.89, median +0.99
5th..95th percentile                  : [-5.24, +6.60]
shuffles matching or beating +4.96    : 12.9%   (p = 0.129)
```

One ordering in eight produces a gain this large from trades with no
loss-to-next-trade link at all. The day-clustered CI on the difference agrees:
**[−0.37, +10.30]**, spanning zero.

**Upsizing after a loss does not create edge. It buys more contracts, which
multiplies whatever edge already exists** - here +0.0030/contract as traded,
which is indistinguishable from zero on 4 days.

### The risk in the table is understated

The observed drawdown of −4.37 is a lucky draw, not the rule's risk profile:

```
max drawdown of the $3 rule across the shuffles:
  median -6.84    5% of orderings worse than -12.91    worst -22.42
  the actual ordering gave -4.37
```

The sample's longest losing streak was **2**. At a 17.1% loss rate, 3 is
expected within 146 trades and longer runs are routine over a month. A 5-loss
run costs about **$12.31** at $3 a trade against **$4.10** flat - 62% of the
$20 `auto_daily_loss_limit` versus 21%.

### What this is, and whose call it is

This is a **leverage decision, not an edge decision**. The rule multiplies
exposure about 1.4x and multiplies the outcome - including the sign. With the
underlying per-contract edge not yet established on 4 days of reconciled
trades, upsizing amplifies a quantity whose sign is still unknown.

The same is true of the shipped $2 step; this measurement does not single out
the $3 proposal.

**Nothing shipped. Sizing is the operator's decision and this is evidence for
it, not a verdict on it.** What would change the reading: enough post-loss
trades to separate 92% from 82.9% - roughly 300-400 of them at this gap, so
weeks, not days.

## 62. The hourly ladder pair: the guarantee is real and already priced (2026-09-24)

Operator's proposal, from the hourly ladder: buy a low strike YES and a high
strike NO, both priced 80-90%, and close 10-15 minutes before expiry. At
least one leg always wins.

**The guarantee is real.** Below the low strike the NO pays; above the high
strike the YES pays; between them BOTH pay. There is no state in which the
pair returns nothing.

### Held to expiry it is exactly zero, and that is algebra

For any pair A < B:

```
cost       = p(X>=A) + (1 - p(X>=B))
E[payout]  = 1·p(X>=B) + 2·(p(X>=A) - p(X>=B)) + 1·(1 - p(X>=A))
           = 1 + p(X>=A) - p(X>=B)
```

Those are the same number. Not approximately - identically, for every pair,
at every strike spacing. All 66 pairs on the operator's own screenshot return
an edge of `+0.000000` before fees; after fees they run from **-0.0028 to
-0.0332**. The circled pair (YES >=83,600 at 0.79 with NO on >=84,200 at
0.94) costs 1.7300 against an expected payout of 1.7300, and loses the
1.57c fee.

The "guarantee" is not an edge. It is a repackaging of the same fair bet,
and what it actually buys is a **capped loss**: -0.75 instead of -1.75.

### Closing early does not escape it

Between the strikes both legs converge on 1.00 and the pair on 2.00; outside
them the pair converges on 1.00. Selling early collects part of that
convergence and pays a second spread for it. Whether it pays depends entirely
on how often the price finishes in the corridor - which is the quantity the
ladder already prices.

Measured on Kalshi's own hourly chains and settlements, 35 settled chains,
entry at 30 minutes, exit at 10:

```
the ladder priced the corridor at : 71.7%
it actually finished inside       : 22/29 = 75.9%
break-even needs                  : 73.5%

payoff when inside  : +0.270
payoff when outside : -0.748   (n=7)
```

2.4 points above break-even - and then:

```
2026-09-21  n= 6  inside 6/6    total  +1.712
2026-09-22  n=21  inside 15/21  total  -0.522
2026-09-23  n= 2  inside 1/2    total  -0.474

day-clustered bootstrap, 3 clusters: 95% CI [-0.2368, +0.2853]  SPANS ZERO
```

**One six-window afternoon in which the corridor held every single time
carries the entire result. The other 23 windows lost money.** The sample is
35 chains spanning 09-21 16:00 to 09-23 02:00 - about 34 CONSECUTIVE hours
of one price path, in which BTC moved 1,664 dollars. Those are not 35
independent draws; neighbouring windows share most of their price history.

### What this actually is

A **short volatility** position. It wins when BTC stays inside the corridor
and loses when it leaves, at roughly 1:2.8 odds against. The ladder prices
the corridor correctly, so the trade is a view that BTC will be more
range-bound than the market thinks - not a free lunch, and not a hedge.

It is the fourth structure tested this session with the same shape: the
hedge (FINDINGS 59), the out-of-the-money take-profit (60), and now this.
Each offers a real-sounding guarantee that dissolves into the fee once the
arithmetic is written out, because both sides of a binary always sum to
1 + spread.

**Decision: not deployed.** Nothing shipped, and the hourly ladder remains a
shadow recorder that never trades. What would change it: a corridor hit rate
persistently above break-even measured across weeks and distinct volatility
regimes, not 34 hours of one quiet stretch.

## 63. The deployed rule on ETH and SOL, with BTC as the control (2026-09-24)

> **WITHDRAWN - see section 65.** The distance here is computed from
> minute-kline volatility, not `brti_normalized_distance`, which is what the
> deployed gate compares. Different quantity, different scale: "10x" selects
> 4% of markets on this scale and 23% on the gate's. Every number below that
> depends on the floor is unusable.

Operator's request: backtest the current live strategy, all features, on ETH
and SOL. No fees, by their standing instruction.

Data already on disk from 2026-09-21: `market_data_kxeth15m.db` and
`market_data_kxsol15m.db`, ~6,400 settled markets each from Kalshi's own
`/markets` and `/markets/candlesticks`, alongside the BTC set. Rule as
deployed today: ask 0.70-0.93, normalized distance >= 10x, retrace <= 0.60,
momentum aligned, entry 660-360s, one entry per market.

### The control first

```
        markets  trades   rate   win     ask    edge/trade   total   days
BTC        6435     259   4.0%  91.1%   0.802     +0.1088   +28.18     43
ETH        6395     116   1.8%  87.9%   0.807     +0.0727    +8.43     23
SOL        6397      28   0.4%  89.3%   0.850     +0.0430    +1.20     15
```

**BTC returns +0.1088 per trade here against +0.0193 per contract that the
live system has actually made since 2026-09-21.** A harness five times more
generous than the thing it models is partly measuring itself, and the
absolute numbers must not be quoted as expectations. The gap is explained:
minute candles instead of ~10s BRTI, no 60s band-hold timer, the minute-close
ask assumed executable, no slippage and no missed fills, one entry taken at
the first qualifying minute, and no fees.

What IS comparable is the ratio, because all three ran through the identical
harness. ETH lands at 67% of BTC's edge, SOL at 40%.

### Both bars

```
day-clustered bootstrap (20,000 resamples of DAYS)
BTC   259 trades  43 days  +0.1088  [+0.0768, +0.1415]  holds
ETH   116 trades  23 days  +0.0727  [+0.0022, +0.1299]  holds
SOL    28 trades  15 days  +0.0430  [-0.0668, +0.1221]  SPANS ZERO

walk-forward, first 60% of days train, last 40% test
BTC   train 170 +0.1126   test 89 +0.1016   HELD
ETH   train  54 +0.0657   test 62 +0.0788   HELD
SOL   train  22 +0.0709   test  6 -0.0590   FAILED
```

**ETH survives both.** Its interval clears zero - barely, lower bound
+0.0022 - and its out-of-sample half scored BETTER than its training half.
That is the first structure tested this session to pass both bars rather than
reverse. **SOL fails both**, on 28 trades and a 6-trade test slice.

### The threshold does not transfer, even where the edge does

```
        markets  qualified   rate   median distance  avg ask
BTC        6435        259   4.0%              11.8    0.802
ETH        6395        116   1.8%              11.6    0.807
SOL        6397         28   0.4%              10.5    0.850
```

The same 10x floor admits 4.0% of BTC markets and 0.4% of SOL. It is not
selecting the same KIND of setup on each instrument; it is selecting a
progressively rarer tail. The floor was measured on BTC volatility
(FINDINGS 43), and a threshold is a statement about one instrument's
distribution. Deploying to ETH means re-measuring the floor on ETH, not
inheriting BTC's - and the 1.8% selection rate means roughly one trade every
two days, which is a different operational proposition from BTC's 4.0%.

### What this did NOT test

**The reversal gate is effectively inert here.** Median retrace is 0.000 on
all three, because at minute granularity the 5-minute retrace window holds
6 points where the live 120-second one holds ~12 at 10s. So this measurement
validates ask, distance and momentum; it says nothing about
`max_brti_retrace`, which shipped today on 79 markets of BTC evidence.

The 60s band-hold timer is also absent, and on BTC it is the single largest
measured improvement in the deployed system.

### Decision

**Nothing deployed.** ETH is the first instrument worth a shadow recorder:
it passed both bars on 116 trades over 23 days, at 67% of BTC's edge through
the same harness. The honest next step is not to trade it but to record it -
book snapshots and a reference series at live resolution - so the distance
floor can be measured on ETH's own distribution and the reversal gate tested
at a granularity that can see it. SOL is not a candidate on this evidence.

## 64. The distance floor refuses setups that were worth taking (2026-09-24)

> **WITHDRAWN - see section 65.** The distance here is computed from
> minute-kline volatility, not `brti_normalized_distance`, which is what the
> deployed gate compares. Different quantity, different scale: "10x" selects
> 4% of markets on this scale and 23% on the gate's. Every number below that
> depends on the floor is unusable.

Operator's observation: KXBTC15M-26SEP241445-45 was refused at 5.5x against a
10x floor, priced 73c, and settled DOWN. The signal was right and no order
went out. They report seeing this often and asked whether it is studied.

**It is archived** - 16,962 refused live setups carry a known outcome, plus
the whole 6,435-market corpus scored against the deployed gates. So the
question is answerable rather than anecdotal.

### A refused winner is not evidence of anything on its own

A 73c contract that wins is the EXPECTED case: 73c is the market's claim that
it wins 73% of the time. What matters is whether refused setups win MORE than
their price implies - a calibration residual, not a win count.

### They do

The setups the 10x floor turns away, everything else at deployed values:

```
setups refused by the 10x floor : 2,696
they won                        : 86.3%
their price implied             : 80.0%
calibration residual            : +6.3%
edge per trade                  : +0.0628
day-clustered 95% CI            : [+0.0485, +0.0768]   excludes zero
```

### And the whole floor is a plateau, not a cliff

```
 floor  trades   rate     win     ask  edge/trade            95% CI   walk-fwd
     3    2955  45.9%   86.7%   0.798     +0.0690  [+0.0560, +0.0820]    +0.0811
     5    1652  25.7%   88.7%   0.813     +0.0739  [+0.0586, +0.0889]    +0.0970
     7     788  12.2%   89.3%   0.814     +0.0797  [+0.0568, +0.1005]    +0.1061
    10     259   4.0%   91.1%   0.802     +0.1088  [+0.0767, +0.1415]    +0.1016
    15      92   1.4%   87.0%   0.783     +0.0864  [+0.0372, +0.1317]    +0.0835
```

**Every floor from 3x to 20x clears both bars** - a day-clustered interval
excluding zero AND a chronological split whose out-of-sample half stays
positive. This is not an in-sample artefact.

### What the floor is actually buying

Per-trade edge RISES with the floor: +0.069 at 3x against +0.109 at 10x. The
gate is doing its job - it selects better setups. What it costs is volume:

```
 10x   259 trades x +0.1088  =  +28.2 total
  3x  2955 trades x +0.0690  = +203.9 total
```

So the floor is a quality-versus-quantity trade, and on this corpus the
quantity side wins by 7x on total dollars while every individual trade is
worth less.

### Three reasons not to simply lower it

**One position at a time.** The account holds a single position, so a 5x
setup taken at 10:00 can block a 12x setup at 10:10. A lower floor does not
only ADD trades, it can spend the slot on the weaker one. Nothing in this
measurement models that, because the corpus scores every market
independently.

**The harness is generous.** It returned +0.1088/trade for BTC at 10x against
+0.0193/contract the live system has actually made - about 5.6x. Minute
candles, no 60s band-hold timer, the minute-close ask assumed fillable, no
slippage and no missed fills. Scaled, 3x's +0.069 is nearer +0.012 live.

**Marginal setups are the ones that miss.** The lower the distance, the
thinner the case, and FINDINGS 22 measured that decision-to-submit latency
already costs fills on setups that DID qualify. A 3x setup is more likely to
move before the order lands, which the corpus cannot see.

### Decision

**Nothing changed.** The 10x floor was set on FINDINGS 43 and this does not
overturn it - it quantifies its cost, which had not been measured before. The
operator now has the number: the refused band is worth +0.0628/trade with an
interval clear of zero, against +0.1088 for what is taken.

The honest next step is not to move the floor but to test a MIDDLE one live -
7x or 8x roughly triples the trade count while keeping per-trade edge within
a cent of 10x - and to measure it against the position slot it actually has
to compete for. ETH already runs 8x for exactly this reason (FINDINGS 63).

## 65. CORRECTION: findings 63 and 64 measured the wrong quantity (2026-09-24)

**Sections 63 and 64 are withdrawn. The floor they recommended was briefly
shipped to BTC and is reverted. No trade was taken under it.**

### The error

`cross_asset.py` computed `normalized_distance` as distance in bps over a
volatility estimated from **minute klines**. The deployed gate compares
`brti_normalized_distance`, computed from the per-second BRTI series by
`features_from_series`. These are different quantities:

```
                                    median   >=10x    >=7x
corpus, real BRTI (what gates read)    5.7   22.9%   39.8%
my harness, minute-kline volatility    3.1    4.1%   11.9%
```

FINDINGS 43 had already recorded exactly this trap in its own words -
"reads 15-22 on BRTI where Binance reads 2-4" - and the same mistake was
made again with klines in place of Binance. A floor of "10x" selects 4% of
markets on one scale and 23% on the other. Setting the live gate to 7x on
the harness's evidence was therefore a far larger loosening than anything
measured, and it reached production for about forty minutes.

### What the right measurement says

Swept with `load_policy_rows(distance_floor=...)` - the loader the TRAINER
uses, on the quantity the gate reads:

```
 floor   taken   rate     win     ask  residual  per trade            95% CI
     6    4963  77.2%   80.9%   0.794     +1.5%    +0.0041  [-0.0063, +0.0143]
     8    4264  66.3%   82.8%   0.810     +1.7%    +0.0066  [-0.0045, +0.0177]
    10    3495  54.4%   84.5%   0.825     +2.0%    +0.0101  [-0.0007, +0.0210]
    12    2738  42.6%   85.9%   0.837     +2.3%    +0.0132  [+0.0010, +0.0255]
    15    1764  27.4%   88.0%   0.852     +2.8%    +0.0197  [+0.0033, +0.0353]
```

**The floor is too LOW, not too high.** Edge rises monotonically with it, and
the interval only clears zero at 12x and above. And the setups 10x refuses
for distance alone are a losing class, not a missed one:

```
refused for DISTANCE ALONE : 547
they won                   : 65.4%
their price implied        : 76.0%
residual                   : -10.6%
per trade if taken         : -0.1184
```

That reconciles with the thing that should have caught this immediately:
**all 21 reject-leg arms in the live policy carry a negative mean.** The
intelligence layer had already measured the refused band on the correct
features and correctly refused to propose a single ADMIT. Section 64
contradicted the running policy and that contradiction was not checked.

### ETH is affected too

FINDINGS 63 chose ETH's 8x floor from the same harness. On ETH's own corpus
with real BRTI features, every floor from 4x to 15x returns a per-trade edge
within 0.003 of zero with an interval spanning zero at all of them. **ETH has
no measurable edge on this evidence at any floor**, which is a different
statement from 63's +0.1034 and supersedes it.

ETH is live and auto-trading. Its record so far is +0.1419, -0.878, +1.2222
across three auto trades - real money, and too few trades to mean anything
either way.

### What was actually wrong with the method

The harness was validated against a control - BTC ran through it beside ETH
and SOL - and the control PASSED, which is what made it feel safe. But a
control only tests the things that differ between arms. Every arm shared the
same wrong volatility, so the comparison between instruments was internally
consistent and the comparison against the deployed gate was meaningless. A
harness must be reconciled against the SYSTEM it models, not only against
itself: BTC returned +0.1088/trade through it against +0.0193/contract live,
a 5.6x gap that was noticed, written down in 63, and then not treated as the
falsification it was.

**The rule this leaves:** when a measurement contradicts a deployed
artefact - the live policy's own arms, in this case - reconcile them before
shipping, not after.

## 66. Three level-holding gates: live said yes, the corpus said no (2026-09-24)

Operator's reframing, after RSI failed: these are mostly in-the-money trades,
so the bet is not "which way will price go" but **"will price stay on this
side of the strike"**. Direction at a 15-minute horizon is already in the
price; whether the strike is being DEFENDED is not.

That reframing is the useful part of this section, whatever the thresholds
turn out to be worth.

### RSI first, and it failed

Tested on the bot's own 181 live executed trades, RSI reconstructed at each
decision instant from Kalshi's per-second series:

```
RSI would KEEP   148 trades   P&L +1.4680
RSI would AVOID   23 trades   P&L +1.4511   <- 20 winners, 3 losers
net effect of the filter            -1.4511
```

Trades RSI disagreed with won **87.0%**; trades it agreed with won 82.4%. The
threshold sweep was incoherent - net -0.77 at margin 0, -5.54 at 5, +5.86 at
20. And the correlation between `|RSI-50|` and `|momentum|` was **+0.030**:
at this horizon it was not measuring what it appeared to.

### Three that did separate on live trades

Same 176 in-the-money executed trades, priced on Kalshi's settlement P&L:

| indicator | what it asks | result |
|---|---|---|
| `accel` | is the move that built the cushion still building? | decaying: 40W/15L yet **-6.93** |
| `held_s` | how long has price held our side? | 5-12 min **91.5%** vs <5 min 79.1% |
| `rejections` | has the strike been tested and turned back? | 2+ **88.1%** vs 1 78.9% |

Shipped together at `accel >= -5`, `held >= 120s`, `rejections >= 2`: kept 59
of 176 (34%), 89.8% win, **+9.21 against +1.07 actually realised**.

Four weaknesses were recorded at the time: 176 trades, 5 days, the best of 36
swept cells, and a trade-level rather than day-clustered interval. `held_s`
was also noted as non-monotonic - alone at 120s and 180s it HURTS.

### The wider test reversed it

5,546 corpus decision points across 68 days, day-clustered, inside the
deployed price band and scaled distance floor:

```
deployed gates only      n=5546  88.3%  +0.0374  [+0.0225, +0.0512]
+ these three gates      n=1628  87.2%  +0.0390  [+0.0166, +0.0606]
what they REFUSE         n=3918  88.8%  +0.0368  [+0.0199, +0.0526]
```

**They refuse 71% of setups and what they refuse scores the same as what they
keep.** And `accel` points the wrong way:

```
decaying < -5 (REFUSED)   n= 653  +0.0410
building >= 0             n=2733  +0.0296
```

`held_s < 120` binds 9 times in 5,546. `rejections` is non-monotonic:
1 -> +0.0366, 2 -> +0.0434, 3+ -> +0.0245 spanning zero.

### The decision, and the pattern

**Kept live by the operator, with that evidence in view.** The recommendation
on the table was archive-only; they chose to keep all three gating. Sizing
and gating are theirs.

This is the third time in one session that a swept winner on a thin sample
reversed on a larger one - sections 63, 64 and now this. The common shape is
exact: a best-of-N cell, a few days, an interval that does not cluster the
thing that is actually correlated, and a result that looks decisive.

What is different here is that the contradiction was found BEFORE it could
be believed, because the features are archived on every decision, qualified
or refused. That is the whole value of recording an indicator's input: it
makes the threshold re-measurable on live data under the current rules
rather than re-arguable. A gate whose input is not archived can only ever be
defended.

**The live prediction to check in a week:** these gates refuse ~71% of
setups. If the corpus is right, live P&L per contract will be unchanged and
trade volume will be a third of what it was. If the live sample was right, it
will improve. The archive will answer it either way.

## 67. RETRACTED BEFORE SHIPPING: "illiquid markets lose" was my own pagination cap (2026-09-24)

The operator listed what Kalshi can supply for expired 15-minute contracts:
ticker and expiry, the UP/DOWN settlement, one-minute bid/ask/price candles,
volume and open interest, and **individual public trades**. Four of the five
were already stored. The trades were not, and they are the only item that is
not another cut of price - `taker_side`, `taker_book_side`, `count_fp` and
`created_time` say who was transacting and which way they leaned. After three
swept winners reversed in one session (63, 64, 66), new information looked
like a better bet than new thresholds on old information.

### What was measured

`scripts/fetch_trades.py` pulled 4,164,733 trades across the 172 markets the
bot has actually traded. Taker imbalance in the minutes before each decision
was a dead end - correlation with the ask -0.021. But the PRESENCE of flow
split the bot's own executed trades hard:

```
flow in the 2 min before entry   n=123  win 94.3%  ask 0.832  residual +11.1%  P&L +24.11
no flow at all                   n= 49  win 55.1%  ask 0.807  residual -25.6%  P&L -20.51
```

Binary, not a gradient: either thousands of trades or exactly zero, nothing
between. It held on all five live days, worse every day:

```
day           flow  noflow   flow win   noflow win
2026-09-21      19      11        89%          73%
2026-09-22      39      19        97%          63%
2026-09-23      43      12        95%          50%
2026-09-24      22       6        91%          17%

no-flow, day-clustered over 5 days: residual -0.256  95% CI [-0.522, -0.143]
```

The interval excludes zero. The effect is enormous. The mechanism was easy to
tell: a quote nobody is trading against is stale, so an 82c setup at 12x
distance is priced off a book no one is honouring. It is independent of every
deployed gate. Everything about it was right except that it was false.

### Why it was false

The fetcher paginated from the newest trade with a 25-page, 25,000-trade cap.
A 15-minute BTC market trades roughly 48 times a SECOND - the windows later
re-fetched properly averaged 13,400 trades per ten minutes - so 25,000 trades
reached back only a few minutes from close, and the cap silently discarded
everything earlier. 137 of the 172 markets hit it.

For those markets the pre-decision window had never been fetched, so the
volume query returned 0, and **zero volume was spelled exactly like an
illiquid market**. Checking the earliest fetched trade against the window:

```
has flow  n=123   window fetched:  92   window truncated away:  31
NO flow   n= 49   window fetched:   0   window truncated away:  49
```

49 of 49. Not one genuine case. The "finding" was my own truncation plotted
against time of day, and the per-day consistency was the cap being consistent.
Nothing was shipped from it.

### What this class of error defeats

Day-clustering, a bootstrap interval, a confound check on session and
time-of-day, a plausible mechanism, consistency across every day in the
sample - it passed all of them. It had to, because the data was not noisy. It
was MISSING, and no statistical test distinguishes a missing measurement from
a measured zero when both arrive as the number 0.

This is a different failure from 63/64 (wrong quantity) and 66 (thin sample
overfitted). Those were errors of inference on real data. This was an error in
the data itself, wearing the costume of a result.

The fix is structural, not a larger cap:

* the fetch is now BOUNDED - `min_ts`/`max_ts` around the decision instant
  taken from `MIN(trade_proposals.created_at)`, reaching backwards, because
  flow after an entry cannot inform that entry;
* `trades_coverage` records the bounds actually covered and a `complete` flag
  set when the page budget outlasted the cursor;
* the analysis DROPS any window not fully covered instead of scoring it, and
  reports genuinely-quiet-but-covered windows as a separate count, so "quiet
  market" stays falsifiable rather than being defined into existence by
  missing data;
* `tests/test_trades_coverage.py` pins the invariant: an unfetched window and
  a real zero must not be representable as the same value.

The 25,000-trade artifact also means the imbalance numbers above were computed
on partly-truncated windows for 31 more markets, so the -0.021 correlation is
not evidence either way. Order flow is UNTESTED as of this section, not
refuted - the re-measurement on bounded windows follows.

### The rule this earns

Before believing any split, ask what the absent case looks like in the data.
If "not measured" and "measured as nothing" are the same value, the split is
not evidence yet, however many days it holds across and whatever the interval
says. Record coverage first; measure second.

## 68. Order flow, tested properly: nothing, and the diagnostic I trusted was wrong (2026-09-24)

Section 67 retracted a liquidity finding that was an artifact of the fetch.
With bounded windows and recorded coverage, 2,067,943 trades across the bot's
173 executed trades and 2,116,757 across 612 corpus markets, order flow can
now be measured honestly. It carries nothing.

### The two claims, and what killed each

**"Illiquid markets lose."** The live version said 49 markets, 55.1% win,
residual -0.256. All 49 were truncation. Properly bounded, NO market the bot
traded was quiet - the quietest quarter still trades ~200,000 contracts in the
two minutes before entry, and the volume quartiles separate nothing (81.4%,
83.7%, 81.4%, 86.4%, every interval spanning zero). Quiet windows do exist in
the corpus, 36 of 612, and they say nothing either:

```
genuinely QUIET (0 trades)  n= 36 days= 4  win 80.6%  -0.0244  [-0.1964, +0.1069]
has flow                    n=576 days=64  win 87.5%  +0.0395  [+0.0120, +0.0643] HOLDS
```

An order of magnitude smaller than claimed, spanning zero, and the 36 fall in
only 4 of 64 days - a day effect wearing a liquidity costume.

**"Balanced flow wins."** This one deserved the wider test on its merits. On
the live trades it had everything section 66 lacked: n=51, residual +0.082,
day-clustered interval [+0.028, +0.114], and a MONOTONIC boundary sweep -
+0.115 at 0.05 easing to +0.013 at 0.20. Refusing the rest would have turned
+3.80 realised into +6.90 on 29% of the volume.

576 corpus markets over 64 days, nearly disjoint from the live days, inverted
it:

```
                      live (5 days, n=51)      corpus (64 days, n=576)
balanced |imb|<0.10   +0.082  HOLDS            +0.0173  [-0.0277, +0.0613]
the rest              -0.015                   +0.0521  [+0.0167, +0.0863] HOLDS
```

Balanced is the WORST bucket out of sample. Both leaning directions beat it
(-0.5..-0.1 -> +0.0531 HOLDS; +0.1..+0.5 -> +0.0678 HOLDS). And the boundary
sweep runs the other way - live tightened into strength, the corpus loosens
into it:

```
|imb| < 0.05   +0.0309  [-0.0296, +0.0877]
|imb| < 0.10   +0.0173  [-0.0277, +0.0613]
|imb| < 0.20   +0.0461  [+0.0136, +0.0770] HOLDS
```

Not shipped. The deployed gates already score +0.0395 [+0.0133, +0.0643] on
these 576; the balanced gate would cut that to +0.0173 while refusing 64% of
the volume.

### The part worth keeping

**Monotonicity is not evidence.** I called the live boundary sweep "what a
real effect looks like, and the opposite of the 66 signature". It was not. A
monotonic sweep on five days is a smooth interpolation through the same small
set of trades; smoothness says the metric is continuous, not that the ordering
will survive. The corpus sweep is equally monotonic and points the other way.
That was the last diagnostic I had that did not require a wider sample, and it
does not work.

What does work is the only thing that has worked all session: a sample large
enough to be split, over enough days, clustered on the day. Sections 63, 64,
66 and now 68 are four five-day winners that reversed, plus 67 which was never
real. The score for five-day findings this session is 0 for 5.

**Why flow could not have helped anyway.** Mean imbalance is -0.117 with a
mean ask of 0.835: we take the favourite, and aggressors buy the cheap side.
The SIGN of taker imbalance is mostly set by which side is cheap - the
favourite-longshot bias of FINDINGS 1 - not by anything about our setup. And
`corr(|imb|, ask)` is +0.015, so the magnitude carries no price information
either. There was no mechanism for it to be informative.

### What the flow data DID establish

Two things, both durable:

* The bot never trades an illiquid market. 0 of 173 executed trades had a
  quiet window, against 36 of 612 corpus markets. Whatever the deployed gates
  are doing, they already exclude the dead end of the book.
* Kalshi sells no historical order-book DEPTH, so outcomes and approximate
  entry prices are testable from candles but queue position and fill
  probability are not. That kills addition counterfactuals - any claim that a
  signal we DIDN'T take should have been taken - which is a second and
  independent reason section 64 could not have been trusted. Refusal
  counterfactuals are unaffected: declining a fill we actually got is priced
  on the broker's own P&L.

The live archive is the only route to the missing half, and it already holds
more than expected: `observations` carries real Kalshi depth from
`orderbook_fp` (`book_yes_depth`, `book_no_depth`, `book_bid_size`,
`book_ask_size`, `book_levels`) on 22,048 rows. Note that `buy_volume`,
`sell_volume`, `trade_count` and `depth_*_qty` in the same table are Binance
BTCUSDT spot in BTC units, NOT Kalshi contract flow, and cannot stand in for
it. `taker_imbalance` there has been 0.0 since the Kalshi-only cutover on
2026-09-23 03:00 UTC, which is deliberate and documented, not a fault.

First cut of the empirical fill model, from our own posted orders - the only
place this can come from:

```
fill rate 91.3%   168 filled / 16 unfilled, 184 matched to archived depth
```

n=16 unfilled is far too few to build a curve, and it cannot answer fill
probability at a price we never posted, which is what an addition
counterfactual needs. It is the right seed and it grows on its own.

## 69. Why every five-day finding failed: the noise floor is twice the edge (2026-09-24)

The session produced five candidate gates from the bot's own live trades and
all five died - 63, 64, 66, 68 reversed on the corpus, 67 was never real. That
looked like a run of bad luck or bad discipline. It was neither. It was
arithmetic, fixed before any of the analysis started.

Residuals of the deployed gates across the corpus, 5,547 decision points over
68 days:

```
mean residual  +0.0375
per-TRADE sd    0.318
per-DAY sd      0.061      <- the relevant one: trades in a day share a path
```

Minimum detectable effect, 80% power, two-sided 5%, clustered on the day:

```
   5 days    MDE +-0.0759     <- twice the entire deployed edge
  20 days    MDE +-0.0379
  64 days    MDE +-0.0212
 120 days    MDE +-0.0155
```

**Five days cannot resolve an effect twice the size of the whole strategy's
edge.** And every finding that reversed measured between +0.08 and +0.11:
balanced flow +0.082, `held_s` +0.111, `rejections` +0.088. Those are not weak
effects that happened to fail. They ARE the five-day noise floor, read off as
signal. A sweep over 36 cells on five days will reliably return a cell near
+0.08 whether or not anything is there, and it will look decisive, because
the bootstrap correctly reports the precision of a quantity that is noise.

This is the unifying explanation for the whole session and it supersedes the
per-section post-mortems: no diagnostic applied to a five-day sample could
have separated these, because the information required was not present. Day
clustering, Holm-Bonferroni, monotonic boundary sweeps and confound checks all
describe a measurement; none of them create resolution.

### The design rule

* **Live data cannot originate a gate.** Its jobs are catching bugs and
  settling predictions the corpus has already made.
* **The corpus proposes; live verifies.** 20 days is the minimum to see the
  deployed edge at all; ~72 days to resolve a change of half its size.
* **State the MDE before the result.** If a candidate's claimed effect is
  below the sample's MDE, the number is not evidence regardless of its
  interval - and if it is far ABOVE, that is itself a warning, since real
  improvements to a +0.038 edge are unlikely to be +0.11.

### What the session actually yielded

Not edge - correctness. The capital inflation that counted 102 settled rows as
$141.25 committed and ratcheted the tier to 2 (fixed: tier went 2 -> 1 live);
ETH reading and, within about six hours, nearly overwriting BTC's policy while
fitting on BTC's corpus; auto cash-out at max profit; the confidence clamp that
absorbed the choppiness penalty so it cost nothing; the `window_open` /
`window_ms` join that produced a confident claim about a fill that never
happened; the fetch-coverage invariant of 67. Every one of those was worth more
than any gate proposed this session, and none of them needed new data.

The deployed rule's edge - +0.0375 over 68 days, interval excluding zero - was
never improved on. It is also the finding most easily lost from view, because
nothing about it changed while a great deal of effort went into things that
did not work.

## 70. Two fixes: money attributed per instrument, and the learned layer made verifiable (2026-09-24)

### The money was the other instrument's, and the account's

Both live instances reconcile the WHOLE Kalshi account into their own ledger.
That is deliberate - the account is one pot and the capital controller has to
see all of it - but `lifetime_record()` read the same unscoped table, so the
performance footer named an instrument and reported the account. The BTC
message read:

```
Live since 19 Sep: -$8.20 · 231 closed · 173W-58L
```

while KXBTC15M itself was **+$0.11 over 211 markets**. The difference:

```
KXBTC15M (bot)   +0.1106   211 markets
KXETH15M (bot)   -1.4360    14 markets
not the bot      -6.7617     7 markets   5x KXBTCD + 2x KXAAAGASD (natural gas)
```

The seven have no `trade_proposals` row and no `manual_trades` row. KXBTCD
matters on its own: the hourly ladder is shadow-only and must never trade.

The error ran in the dangerous direction - it made a flat strategy look like a
losing one, which invites changing something that is working. `store.py` now
scopes the figure to `settings.kalshi_series` via `configure_instrument`, and
reports the remainder as what it is rather than absorbing it:

```
KXBTC15M:  Live since 19 Sep  +0.3900  over 213 markets  166W-47L
           account also holds -8.1977 in 21 market(s) this strategy did not place
KXETH15M:  Live since 24 Sep  -1.4360  over  14 markets    9W- 5L
```

ETH's label self-corrected from "Live since 21 Sep" to 24 Sep - it had been
inheriting BTC's first market. An unconfigured Store still reports the whole
ledger, so tests and any single-instance deployment are unchanged. The series
match is by `"{series}-%"` prefix, which `test_instrument_money.py` pins
specifically because KXBTC15M and KXBTCD share a prefix up to the D.

### The learned layer: on, acting, and now checkable

Operator's instruction: the intelligence is on and affects trades, never off.

It already was, and the earlier report in this session that it "has never
touched a trade" was wrong. Vetoes and admissions are off, but the layer acts
through the CONFIDENCE channel, and confidence decides execution at the HIGH
threshold. The ETH fill at 18:49 existed only because of it: 80 + `learned +7`
= 87, HIGH at 85. Graded on outcomes, BTC's deltas order correctly:

```
delta  -6:  241 seen,  53 filled,  161W/80L  = 66.8%
delta  +4:   39 seen,  35 filled,   31W/ 8L  = 79.5%
delta  +5:   60 seen,  60 filled,   47W/13L  = 78.3%
delta  +8:  155 seen, 154 filled,  129W/26L  = 83.2%
```

A 16-point spread, and it is acting on it - 154 of 155 filled at +8 against 53
of 241 at -6.

**ETH's is inverted and this is recorded beside the decision, not against it:**

```
delta  +4:   7 seen,  7W/ 0L
delta  +7:  10 seen,  7W/ 3L
delta +10:  30 seen, 15W/15L   <- largest bonus, coin flip
```

ETH has one bootstrap fit on its own 6,389-market corpus and no graded live
evidence. The recommendation on the table was to suppress ETH's delta until it
has some; the operator's instruction is that the layer stays on everywhere. It
stays on. This is the number to re-read once ETH has a week of gradings.

Vetoes being off is the promotion bar working, not a switch being down: no arm
clears Holm-Bonferroni at FWER 0.05 on the validation slice, and graded live,
all three current veto arms would refuse more winners than losers (7W/0L,
23W/6L, 7W/5L). Enabling them is a decision about evidence.

### Why "never off" needed code, not a flag

`intelligence_enabled=True` says the operator wants the layer on. It does not
say the layer CAN act. Five conditions reduce every decision to NEUTRAL while
every switch still reads on: a missing or empty artefact, a feature-fingerprint
mismatch, evidence older than `intelligence_max_policy_age_ms`, a mode other
than `live`, and every arm withdrawn. Each is correct behaviour alone - a stale
fit describing a market that has moved on SHOULD stand down - but a layer
contributing +0 is indistinguishable from a layer that is gone, so "it is on"
could only ever be believed.

`intelligence_policy.health()` answers it as a state, `main.active_policy`
publishes it on every load and reload, and `surface.compose` - the single
assembler - renders a warning when the layer cannot act, so no message builder
can omit it. It is silent while healthy on purpose: a reassurance printed on
every message stops being read.

Verified live on both instances, which are correctly separate:

```
BTC  version=kalshi-brti-2-1790275749  arms=29  acting=2  age=0.2d  ok=True
ETH  version=kalshi-brti-2-1790275536  arms=30  acting=2  age=0.2d  ok=True
```

1,394 tests pass (20 new). Both instances restarted on the change: BTC 14728
via the watchdog, ETH 14476 manually, preflight FLAT at the time.

### Resolved: the 7 unattributed markets were manual (2026-09-24)

The operator confirms the five KXBTCD and two KXAAAGASD markets, -$6.76
between them, were placed by hand. **No defect**, and specifically not the
hourly ladder breaking its shadow-only guarantee - which held.

The general fact this establishes, worth more than the resolution: an
operator-placed position in ANY series lands in both instances' ledgers with no
`trade_proposals` and no `manual_trades` row. So absence from those tables means
"this system did not place it", never "something placed it that should not
have". That is now the fix's whole purpose - the footer separates the account
from the strategy, so hand trading and bot trading can coexist in one account
without either being mistaken for the other.

Final attribution:

```
KXBTC15M (bot)   +0.3900   213 markets  166W-47L
KXETH15M (bot)   -1.4360    14 markets    9W- 5L
operator, by hand -6.7617     7 markets
```

## 71. The winning margin: 18.4 bps on both instruments, and now in the lifecycle (2026-09-24)

Operator's question: when a trade wins, how far does the market travel from the
target to where it settles - for BTC and for ETH?

### The answer

```
              n     mean $      median $    mean bps   median bps
BTC winners  146   +$157.44    +$128.58       18.4        15.1
BTC losers    29    -$67.13     -$35.66        7.9         4.2
ETH winners    9     +$4.91      +$4.37       18.3        16.3
ETH losers     5     -$1.74      -$0.96        6.5         3.6
```

**18.4 bps on BTC and 18.3 on ETH** - assets 20x apart in price, the same
figure to a decimal. The margin is scale-free, so bps is the only honest unit:
$128 on BTC and $4.37 on ETH are the same event and dollars cannot say so.

The asymmetry is the finding. Winners finish about **2.4x further past** the
target than losers finish short of it, and the medians widen that to 3.6x
(15.1 vs 4.2 bps on BTC). When this strategy is wrong it is barely wrong -
median miss $35.66 on BTC, $0.96 on ETH.

That is the exact signature of buying 83c favourites: the losses are
NEAR-MISSES, not reversals. It also explains why every filter tried this
session refused more winners than losers. A filter can only separate what is
separable, and at settlement the losers sit 4 bps on the wrong side of a line
the winners clear by 15 - which is inside the noise of any pre-entry feature.
The 18.3-vs-6.4 asymmetry and the 0-for-5 record of section 69 are the same
fact seen from two ends.

### It is recorded per trade now, from Kalshi

Both numbers come off Kalshi's market object - `floor_strike` is the target the
signal message already quotes, `expiration_value` is where the settling BRTI
finished. Taken from the BROKER rather than recomputed from
`settlement_reference.db`, which holds equivalents and would have been quicker
to read: the archive is ours and the settlement is Kalshi's, and where they
disagree the broker is right by definition. Same rule as the P&L.

* `settlements` gained `strike`, `expiration_value`, `facts_synced_ms` by
  `_add_columns` migration - `CREATE TABLE IF NOT EXISTS` would have reached
  only fresh installs;
* `execution.settlement_facts()` reads the public market object, returning {}
  rather than raising, because enrichment is bookkeeping and must never
  interrupt a poll with money in it;
* the service enriches 8 per pass, so a backlog is worked off over several
  polls, and marks a market asked even when Kalshi publishes neither number -
  otherwise the queue never drains;
* `store.settlement_margin(ticker, side)` returns it from OUR side's view, so
  positive is favourable for UP and DOWN alike, in dollars and bps;
* `scripts/backfill_settlement_facts.py` filled history: 235 markets per
  instance, zero failures;
* the recap carries `Settled $127.50 past the target · 18.3 bps`, with landing
  exactly on the strike named rather than rounded away.

**A latent bug had to be fixed to do this safely.** `record_settlements` was a
positional `INSERT OR REPLACE ... VALUES (?,?,...)` with thirteen placeholders.
Widening the table would have written values into the wrong columns, and every
routine P&L re-sync would have blanked the new facts - a field that is always
present and always empty, which is the failure mode FINDINGS 54 already
recorded twice. It is now a named-column upsert that touches only the columns
it lists, pinned by `test_a_pnl_resync_does_not_wipe_the_facts`.

Deployed with the account FLAT (it was not on the first attempt; the restart
waited). 1,408 tests pass, 14 new. All 237 settlements in both databases carry
the margin, and the two that settled AFTER the restart were enriched by the
live service rather than the backfill - the live path is confirmed working, not
assumed:

```
KXBTC15M-26SEP242030-30  strike 84630.33  value 84639.58  move   +9.25
KXETH15M-26SEP242030-30  strike  2694.23  value  2691.05  move   -3.18
```

### Loss step reduced $5 -> $2 by the operator (2026-09-24, same day)

"5 is too risky just to make 50." The reduction is well founded on the
measurement that shipped the $5 version in the first place (section 61): the
whole +8.85 gain came from 25 post-loss trades winning 92.0% against 82.9%
overall, which a permutation test could not separate from ordering luck
(p=0.249). And the DOWNSIDE was the least-evidenced number in that result - the
sample held two 2-loss runs and no 3-loss run, so the -5.80 drawdown it showed
had never been tested by the thing that would move it; across shuffles the
median worst drawdown was -6.84 and the worst -22.42.

Sizing down where the evidence is thinnest is the conservative reading of
exactly that result, and the ratio the operator objected to - risk per
post-loss trade against a speculative total upside - is a question the backtest
never asked.

Effect across the deployed price band:

```
ask 0.70 -> 2 contracts, $1.40 at risk   (was $4.90 at $5)
ask 0.80 -> 2 contracts, $1.60 at risk   (was $4.80 at $5)
ask 0.93 -> 2 contracts, $1.86 at risk   (was $4.65 at $5)
```

`loss_step_max_contracts` stays at 8 deliberately: it bounds the other upsize
paths too, and lowering a ceiling this rule can no longer reach would only look
like a tightening. $2 now matches the other upsize triggers, so the three no
longer disagree about what one step up means. Both instances restarted FLAT;
1,409 tests pass.

## 72. Gold 15-minute: the rule does not transfer, and gold cannot answer whether it should (2026-09-24)

`KXGOLD15M` exists, settles the same way BTC15M does - strike is the previous
window's settlement, "above or below in 15 minutes" - and carries the SAME
per-second reference feed at `/live_data/events/{event_ticker}`: 3,600 points
at 1s in the identical `{t, v}` shape. Band liquidity matches too: 34.2% of
candles inside 70-93c against BTC's 32.2%, median spread 1.0c on both. Every
structural precondition for running the deployed rule is present.

It still does not transfer, for a reason visible before any backtest.

### The distance floor is unreachable on gold

```
        vol_bps   |mom|   norm_distance   clears 10x   clears 15x
GOLD       5.67    3.78            0.83         0.6%         0.5%
BTC        0.77    4.28            6.92        33.4%        15.7%
```

The rule requires the reference to sit 10-15 VOLATILITY UNITS from the strike.
Gold's per-second feed is about seven times noisier in bps than BRTI, so the
same dollar gap buys a fraction of the volatility units. Median normalised
distance is 0.83 against BTC's 6.92, and the deployed floor would fire on
roughly three of 584 markets. That is not a weak result; it is no result.

This is the third instrument to confirm the same thing: a threshold expressed
in volatility units is still an instrument-specific constant, because it
inherits the noise characteristics of that instrument's reference feed.

### Gold's own scale, measured on its own percentiles

1,630 priced decision points in the band over 35 days:

```
bucket                           n  days    win%     ask residual         95% CI
everything in the band        1630    35   83.7%   0.813  +0.0240  [-0.0142, +0.0605]
distance p0-25                 407    33   74.9%   0.741  +0.0089  [-0.0467, +0.0642]
distance p25-50                408    32   83.8%   0.790  +0.0479  [-0.0136, +0.1021]
distance p50-75                407    34   86.7%   0.842  +0.0254  [-0.0256, +0.0691]
distance p75-90                245    33   87.3%   0.880  -0.0061  [-0.0691, +0.0482]
distance p90+ (farthest)       163    31   92.6%   0.883  +0.0436  [-0.0175, +0.0968]
```

The favourite-longshot bias IS present - 83.7% at an 81.3c ask, +2.4% residual,
the same direction and a similar magnitude to BTC's. What is absent is the
DISCRIMINATOR: distance buckets run +0.048, +0.025, -0.006, +0.044. Not
monotonic, no ordering, nothing clears zero. The mechanism the deployed rule
depends on has no signal in gold.

### Why this cannot be resolved with more effort

```
        n     days   residual   per-day sd   MDE at n days
GOLD   1630     34    +0.0240      0.1170        +-0.0562
BTC   19087     68    +0.0181      0.0423        +-0.0144
```

**Gold's per-day residual sd is 2.8x BTC's.** Its observed +0.0240 sits well
INSIDE its own noise floor, and resolving an effect that size would take **187
days** against BTC's 43. We have 34, and cannot get more: the reference
endpoint serves only recent events, so the backfill returned 584 of 3,907
markets with 197 fetch failures and no path to the rest.

So the verdict is not "gold has no edge". It is that gold cannot be measured to
a conclusion with the data that exists, and the one thing that could have been
measured - whether distance discriminates - came back flat.

### The operator's live test

A manual gold trade was taken at 97c with the reference $5.81 above a $4,287.96
target and 7 minutes left. Worth recording that the deployed rule would have
DECLINED it on price alone: the band ends at 93c. At 97c a win pays 3c against
97c at risk, which needs a 97% strike rate merely to break even - and gold's
measured win rate in its FARTHEST distance decile, the most favourable bucket
in the table above, is 92.6%.

## 73. Gold: the operator was right, and the edge is where BTC's rule would never look (2026-09-24)

Section 72 concluded gold could not be measured to a conclusion. That was
wrong, and it was wrong because it asked gold BTC's question.

### The operator's correction

Told that gold's reference was "seven times noisier", the operator pushed back:
*"gold is more stable at maintaining its price level than any other asset"*.
Both statements are the same measurement, and the operator's framing is the
useful one. Share of short-horizon volatility that survives to settlement:

```
GOLD  0.040      BTC  0.305
```

Gold's per-second feed genuinely jitters - 80% distinct values, 1.5% flat
ticks, so it is not quantisation - but that jitter MEAN-REVERTS. Gold holds its
level eight times better than BTC. Measured directly, with no model at all:

```
        |15-min move|   median distance   distance/move
GOLD       5.21 bp          3.85 bp          0.74x
BTC        5.57 bp          4.34 bp          0.78x
```

The two are equivalent in risk-adjusted terms, while
`brti_normalized_distance` reports 0.83 against 6.92. **The denominator was
measuring feed noise, not tradeable volatility**, and the 8x correction is
confirmed by two independent routes (persistence ratio 7.6x, direct
distance/move 7.9x).

### Where gold's edge actually is

Level-maintenance shows up exactly where it should - P(side holds to
settlement) by distance already travelled:

```
distance      GOLD     BTC    gold edge
0-2bp        57.7%   59.1%       -1.4
2-5bp        75.4%   66.0%       +9.4
5-10bp       78.4%   76.3%       +2.1
10-20bp      86.8%   84.0%       +2.8
20bp+        86.0%   89.1%       -3.1
```

BTC needs 10-20bp to reach 84%. **Gold reaches 75% at 2-5bp** - and the market
does not price the difference:

```
asset  distance     n    win%     ask  residual                95% CI
GOLD   2-5bp      865   75.7%   0.686  +0.0715  [+0.0344, +0.1078]  HOLDS
BTC    2-5bp     8921   71.5%   0.708  +0.0069  [-0.0098, +0.0227]
```

Gold at 2-5bp is priced like BTC at 2-5bp and outperforms it by four points of
win rate. **+7.15% residual, against the deployed BTC strategy's +3.75%.**

### It survives every test that killed the others today

```
p = 0.0001; Holm-Bonferroni over the 8 cells needs p < 0.00625   SURVIVES
boundary: all 7 overlapping bands hold, +0.0606 to +0.0787
sub-period: first half  +0.0576 [+0.0120, +0.0981]  HOLDS
            second half +0.0865 [+0.0285, +0.1415]  HOLDS
sessions:   all four positive, +0.0567 to +0.0860
fillable:   tight spread (<=1.0c) +0.0771 [+0.0376, +0.1141]  HOLDS
            wide spread  (2.5c)   +0.0310 [-0.0628, +0.1145]
```

The DISJOINT sub-period split is the one that matters - it is what reversed
sections 63, 64, 66, 68 and the perpetual plateau. Both halves hold
independently. And the edge concentrates in TIGHT books, the opposite of a
stale-quote artifact; that check is what condemned the perpetual basket.

### Why BTC's rule could never have found this

The deployed rule requires 10-15 normalised distance units and an ask of
70-93c. Gold's edge sits at 2-5bp of distance and a **0.686 ask** - below the
price band and, on the uncorrected scale, far below the distance floor. The
rule would have rejected every one of these 865 setups twice over.

An instrument's gates are not portable, and this is the sharpest instance yet:
the transfer failed not because gold lacks an edge but because BTC's thresholds
point away from it.

### What is still unknown

Thirty-four days. Fill probability at 0.686 is unmeasured - Kalshi serves no
historical depth, so this is an ADDITION counterfactual and section 68's
limitation applies in full: it claims setups we have never traded. The corpus
is 584 of 3,907 markets because `live_data` serves only recent events, so the
sample may not be representative of the rest.

Nothing is shipped. The next step is not code: it is more days, and then a
paper-traded confirmation that the 0.686 ask is actually fillable.

### Gold shipped live as a third instance (2026-09-24)

`runtime-gold/`, `gold15.db`, `strategy_kalshi_gold.json`,
`scripts/run_gold.ps1`. BTC and ETH untouched; all three now on the same
revision.

**Every number in gold's config was measured on gold and none inherited.**

```
ask 0.65-0.75        entirely BELOW where BTC's 0.70-0.93 starts
distance 1.5-6.0bp   a BAND in bps, not a floor in volatility units
entry 400-700s       below 6.7 minutes the edge dies (-0.0068)
```

Two structural departures from BTC, each load-bearing:

*The distance test is a band.* On BTC more distance is always better - it means
the move already happened and the strike is far behind. On gold the edge is
level-MAINTENANCE, so distance beyond 6bp is evidence against the trade:
2-5bp scores +0.0715 while 5-10bp scores +0.0074 spanning zero. Implemented as
`min_abs_distance_bps` / `max_abs_distance_bps`, both None by default so BTC
and ETH are bit-identical.

*An unmeasurable retrace is not a refusal on gold.* `brti_retrace` is None
whenever the recent window holds no advance to give back - which on BTC means
the move cannot be shown alive, and on gold IS the target state. The shipped
gate would have refused precisely the setups the edge was measured on. Added
`require_measurable_retrace`, default True.

**BTC's three level-holding gates are OFF on gold, measured not forgotten.**
`accel` refuses the better group (+0.1334 refused against +0.0908 kept); all
three together cut volume 51% to buy +0.009 of residual, dropping total edge
from 52.0 to 27.7. Momentum and retrace are ungated because they are not yet
measured on gold - this codebase's own rule being that an unmeasured gate is
worse than none, since it looks deliberate.

**A latent defect found while wiring it.** `surface.asset()` knew only crypto
names, so `KXGOLD15M` returned "". That is not cosmetic:
`learning_runner._corpus_mismatch` treats an unrecognised series as "no
opinion" so a synthetic corpus is not refused - which means gold's corpus guard
was SILENTLY DISABLED and a fit on BTC rows would have been allowed. Exactly
the failure that guard exists to prevent. GOLD, SILVER, PLATINUM and PALLADIUM
added; pinned by a test that feeds a gold instance BTC rows and asserts refusal.

**Verified live.** Gold bootstrapped its learning on **560 markets** - its own
corpus, not BTC's 6,428 - producing 13 arms, 0 promoted. Each database writes
only its own instrument's tickers (BTC 343, ETH 43, GOLD 1). Three distinct
policies: 29 / 30 / 13 arms. 1,427 tests pass.

**What is not yet known, and will not be known for weeks.** Gold's corpus is
584 of 3,907 settled markets because `live_data` serves only recent events, so
the sample may not represent the rest. Fill probability at a 0.694 ask is
unmeasured - Kalshi sells no historical depth, so this is an ADDITION
counterfactual and section 68's limitation applies in full: it claims setups
never traded. 33 days is short, though the effect is large relative to gold's
noise floor and survived a disjoint sub-period split, which is the test that
reversed sections 63, 64, 66, 68 and the perpetual plateau.

The prediction to check: ~16 trades a day at a 0.694 ask and 79.3% win. If the
corpus is right the residual stays near +0.09; if the sample was unrepresentative
it will fall toward zero. The archive answers it either way.

## 74. WITHDRAWN: "the gates refuse profitable setups" was a reporting defect (2026-09-25)

### What was claimed, and is now withdrawn

Three claims made on 2026-09-24/25, all from the same source and all wrong:

* that BTC's gate was ANTI-SELECTING - qualified -2.1% against declined +3.5%;
* that since the three level-holding gates shipped, BTC had qualified only 4
  signals in 14 hours while what it DECLINED scored +7.7%;
* the section 66 formulation that its gates "refuse setups that were worth
  taking", insofar as it rested on live declined-vs-qualified counts.

The operator identified the defect before it changed anything: *"A declined
contract eventually winning does not prove it offered a qualifying, profitable
entry beforehand."* Exactly so.

### The defect

`predictions.qualified` is a snapshot written when the ALERT was produced.
Every report read it as the market's verdict, which classifies a 15-minute
contract by its FIRST evaluation. It is a real fact about the alert; it was
being used as a fact about the market.

Traced against `intelligence_decisions`, which records every poll:

```
markets that failed the distance check -> did evaluation CONTINUE?
  BTC 55/55      ETH 49/49      GOLD 37/37        (stopped: 0, 0, 0)
median evaluations per market: 27

of those, LATER became eligible:  BTC 11   ETH 14   GOLD 32
markets that became eligible -> order placed -> FILLED:
  BTC 13/13      ETH 14/16 (1 unfilled, 1 awaiting authorisation)
```

**There was no lifecycle bug.** A failed gate rejects that moment and nothing
more, evaluation runs to the entry deadline, and when a market becomes eligible
an order goes out and fills. The trading path was never at fault - only the
reporting of it.

The scale of the misclassification:

```
BTC: 53 predictions marked "not qualified" -> 9 had actually TRADED, 9W/0L at 0.829
ETH: 55 marked "not qualified" -> 10 had actually TRADED, 10W/0L at 0.852
```

Because every misclassified market was a WINNER, moving them from "declined" to
"traded" moves a block of wins out of one bucket and into the other. That single
error produced both halves of the false conclusion.

### The corrected evaluation, since the last gate shipped

Classified by whether a market was EVER eligible:

```
              ever eligible                    never eligible
BTC    n=13   92.3%  price 0.838  +8.5%    n=45  77.8%  price 0.734  +4.4%
ETH    n=16   93.8%  price 0.834 +10.3%    n=43  76.7%  price 0.763  +0.5%
GOLD   n=32   71.9%  price 0.711  +0.8%    n= 5 100.0%  price 0.776 +22.4%
```

Gold's corrected figure is +0.8%, not the +9.3% reported from the snapshot -
that number compared different populations. Gold's 32 eligible markets are all
`awaiting_authorization`: auto-execution is off there, which is a deliberate
SETTING and not a failure of anything.

### What this does and does not establish

It supports the gates' SELECTION so far. It does not show they are effective.
The samples are 13, 16 and 32 markets; a residual here is win rate minus quoted
price, which is calibration and not net executable profit; it excludes fees and
assumes a fill at the quoted price, which is the addition counterfactual
section 68 established cannot be verified. Whether the gates are worth their
cost in refused volume is a separate question these samples cannot answer.

### What was built

`src/btc15_signal/market_lifecycle.py` derives, per market: eligibility
(never / at first / became, with the first eligibility instant and every
transition), execution (no proposal / awaiting authorisation / blocked /
submitted-unfilled / partially filled / filled, with reasons and order ids),
and outcome (won / lost / PENDING / no record, never merged).
`scripts/lifecycle_report.py` reports on it.
`tests/test_lifecycle_classification.py` (13 tests) pins the contract, the
first of them being the exact mistake in the exact shape it occurred.

**Training labels were never affected.** `learning_data` already labels on
`intelligence_decisions.base_qualified` per evaluation and upgrades a market
when a later poll qualifies - the became-eligible semantics, correct all along.
Only the reporting was broken, so no label regeneration is required.

`since_ms` filters on DECISION time, not window open. Mixing the two anchors is
what made 16 eligible ETH markets report as 15; reconciled, all 16 are graded,
0 pending, 0 missing.

### A second error, recorded because it was mine

While adding the module I wrote it to `src/btc15_signal/lifecycle.py`, which
already existed and holds the trade-path reconstruction (`exit_quotes`,
`build_lifecycle`, MFE/MAE, exit simulation) that `gridsearch` and `validation`
import. The Write result said "updated", not "created", and I did not read it.
It was restored byte-exact from git - `git status` shows the file unmodified -
and the new module renamed to `market_lifecycle.py`. Check whether a module
exists before writing it; the tool result says which happened.

### 74a. Four facts, kept apart, and none of them inferred (2026-09-25)

Three further corrections from the operator, each removing an inference that
was being presented as a record.

**`pending` is a lapsed state, not an open one.** Every resting proposal on all
three instances is past its expiry - BTC 328, ETH 72, gold 38, zero live, none
ever carrying an order id, expiry set about two minutes after creation. Reported
as `expired`.

**Automation being on does not prove an approval was requested.** It means
approval was not REQUIRED. There is no approval-request record anywhere in the
schema - notification kinds are settlement, signal, fill, cash_out,
session_close, learning and recovery - so `awaiting_authorization` cannot be
evidenced today and must not be claimed. The module had been inferring it from
the automation setting, which is exactly the error the whole section is about.

So four facts are now separate fields, and merging any two reintroduces the
ambiguity:

```
execution          expired | filled | submitted_unfilled | blocked | ...
automation_on      the setting as it stood
authorization      requested | approved | declined | never_requested
execution_reason   evidence-backed only
```

A reason is stated only where a record supports it. With automation off, the
absence of any submission IS the evidence and the report says so. With
automation on and nothing explaining the lapse, it says **"expired without
submission - reason unknown"** rather than naming a cause.

**Proposal expiry does not terminate market evaluation**, verified rather than
assumed. Of markets whose entry window stayed open past a proposal expiry:

```
        window still open past expiry   evaluation continued   became eligible AFTER it
BTC                   160                       160                      52
ETH                    74                        74                      17
GOLD                   31                        31                      15
```

Every one continued, and a substantial share became eligible afterwards. A
lapsed proposal ends that ATTEMPT, not the market.

**Counts are timestamped.** They move as markets settle and fills land - gold's
eligible set went 32 -> 33 and ETH's fills 14 -> 15 between two runs an hour
apart - and an untimestamped count invites reading that as a reconciliation
error rather than a later snapshot. Every report now prints the instant it was
taken, plus the automation setting and the authorization tally.

1,450 tests pass. Nine of them pin this section, the sharpest being that
automation being on never implies an approval was requested.

## 75. Seven marginal best-buckets intersect to nothing: all three configs refitted as SETS (2026-09-25)

### The error, and it was mine

Silver and SOL each received seven thresholds, and every one was chosen from its
own best bucket in isolation — price 0.50-0.60, distance ≤2bp, held ≥60s,
rejections ≥10, momentum ≤10bp, accel ±10, plus a retrace gate inherited from
BTC. Each was defensible alone. Intersected, they admitted almost nothing, and
the check that would have caught it — *does the COMBINATION let a usable share
through* — was never run.

Measured against each instrument's own corpus, the deployed sets admitted:

| deployed config | admits | win rate | residual |
|---|---|---|---|
| SILVER | 2 / 4,017 = **0.05%** | unscoreable | — |
| SOL | 235 / 10,878 = **2.16%** | **50.6%** vs 73.9% baseline | **-0.0502** |
| GOLD | 705 / 4,524 = 15.6% | 78.6% | +0.0906 |

Live it was worse: 423 and 422 evaluations, zero qualified, every market
blocked. Gold, carrying two active gates, qualified 18.7%.

SOL is the sharper lesson. It was not merely narrow — what little it let
through it chose **badly**, at a 50.6% win rate against a 73.9% baseline. A
config can be wrong in both directions at once.

### Two things the corpus could not see

**`brti_retrace` and `brti_choppiness` were never stored.** `features_from_series`
computes both and the backfill discarded them, so every fit ever run scored
candidate sets as though those two gates were absent, while live enforced BTC's
inherited `(0.60, require-measurable)`. That is most of the gap between "the
corpus says 7.2%" and "live says 0%" — under the real rule SOL's old config
admits 20 of 6,396, not 235. Both columns are now recorded.

**`enabled` was rendered but never computed.** `surface.check_line` renders a
gate as DISABLED when `fact["enabled"] is False` and `checks_summary` excludes
it from the count. Nothing ever set it: `check_facts` called
`fact.setdefault("enabled", True)`, so every gate came back enabled and the
rendering was unreachable. The defect the gold audit was raised about — "Entry
checks 8/8" with five thresholds that no input can fail — was therefore never
actually fixed. `KalshiBRTIRule.gate_binds` now derives it from the config, and
gold's alert reads `3/3 (5 off)` on the old config, which is what the operator
said it should have said all along.

### What replaced the method

`scripts/fit_instrument_config.py` scores complete candidate sets, never single
gates, against three requirements in order:

1. **Volume is a constraint, not a preference.** A set admitting under 15% can
   never reach `min_evidence`, so the learned layer stays inert and the
   configuration can never be corrected by evidence.
2. The set's own residual interval clears zero.
3. **Gating demonstrably beats not gating** — the gated-minus-ungated
   difference, bootstrapped on the same resampled days.

Requirement 3 exists because of the *opposite* failure, which I walked into on
the way here: ranking by total edge chose a set admitting 74% of gold's points
at +0.0495 against a +0.0346 baseline. That is the baseline with extra steps. It
selects for nothing, and without a paired test it looks like an improvement.

Sets are then tiered — A holds in both disjoint halves, B in one, C spans zero —
and a lower tier is reported only when the one above is empty. That is the bar
gold was originally shipped on and the one silver and SOL skipped.

### An accident that turned into a holdout

Re-fetching the reference series to capture retrace landed on a **near-disjoint
sample of the same period**: gold's two corpora share 59 markets out of 485 and
365, because the stride walked a market table that had grown. The two disagree
by **0.042 on the ungated baseline** — as large as most edges being fitted here.
That is the most useful number in this section. It sets the scale of sampling
noise in a 36-day window, and it is why the fits use both corpora merged (gold
758 markets, silver 677, SOL 1,844) and why each set is then re-scored on the
sub-samples separately.

### The three results

| | tier | share | win | residual | vs ungated | halves | replicates |
|---|---|---|---|---|---|---|---|
| **GOLD** | A | 20.4% | 78.4% | +0.0925 [+0.0613, +0.1219] | +0.0743 [+0.0522, +0.1001] | both hold | **yes**, +0.1003 [+0.0634, +0.1381] |
| **SILVER** | A | 17.8% | 78.2% | +0.0562 [+0.0222, +0.0891] | +0.0373 [+0.0058, +0.0666] | both hold | **no**, +0.0340 [-0.0098, +0.0764] |
| **SOL** | C | 22.1% | 73.6% | +0.0222 [-0.0057, +0.0486] | +0.0209 [+0.0001, +0.0411] | one decays | no |

**Gold is established.** It holds on both sub-samples, both halves, and gating
beats not gating on each. Three gates that were sentinels — `accel >= -1e9`,
`held >= 0s`, `rejections >= 0` — now carry measured values, and the distance
band came back as the same 1.5-6.0bp it already had, which is the strongest
thing that can be said for a threshold.

**Silver is provisional.** It clears every requirement on the fitted sample and
both halves, but on the older sub-corpus alone it spans zero: more than half the
measured edge lives in one of the two samples. Gold's equivalent check held on
both. Automation stays off.

**SOL has no edge, and 1,844 markets say so.** Its ungated residual is +0.0013
[-0.0124, +0.0159] — the market prices these contracts correctly. Of 4,860
candidate sets, 1,073 cleared volume and the baseline and **not one**
demonstrably beat taking everything. Two near-disjoint samples, 57 and 68 days,
agree. Its config now exists to RECORD, not to select, and says so.

### What this does not establish

The residual is win rate minus the price quoted — calibration, not realised
profit. It excludes fees, per the operator's standing instruction, and it
assumes a fill at the quoted price, which for a market no order ever touched is
a counterfactual this system cannot verify: there is no historical book depth.
Gold's live automation state is unchanged, and none of these numbers is an
argument for turning it on.

The configs hot-reload — `KalshiBRTIRule.load` runs every evaluation cycle — so
all three took effect without restarting anything.

### The test that was missing

`tests/test_gate_sets_admit_volume.py` runs the real rule over each
instrument's own corpus and asserts three things per instrument: the set admits
at least 10% of decision points, it does not admit at a worse win rate than it
blocks, and no gate counted as a passed check is one that nothing in the corpus
can fail. It fails loudly on the old silver and SOL configs, which is the point.

One correction inside that test, recorded because it made the guard useless:
`sum(1 for _, won in through)` counts every row rather than the wins, so the
"does not select for losers" assertion read 100% for both instruments and could
not fail.

Admitted against blocked, under the real rule including retrace:

| | admits | admitted W-L | admitted win | blocked win |
|---|---|---|---|---|
| GOLD | 17.4% | 279W-86L | 76.4% | 71.5% |
| SILVER | 15.5% | 213W-63L | 77.2% | 73.9% |
| SOL | 20.5% | 962W-346L | 73.5% | 73.9% |

SOL's two columns being equal is not a failure of the gating — it is the same
finding as the table above, seen from the other side.

### 75a. The corpus holds only markets whose fetch succeeded (2026-09-25)

An open caveat on every number in section 75, found while checking the fits
against fresh reference data and not resolved.

The corpus and an independent replay disagree about the same 8 days. On markets
present in both, gold's deployed set scores **+0.1019 [+0.0423, +0.1883]** from
the corpus and **+0.0069 [-0.0213, +0.0558]** from the replay. Three
explanations were tested and two are eliminated:

**Not staleness.** Stored features were compared against features recomputed
today for 270 decision points across 45 markets: `signed_distance_bps`,
`brti_momentum_bps`, `brti_volatility_bps`, `brti_normalized_distance`,
`brti_accel`, `brti_held_s` and `brti_rejections` all matched **exactly, 100%**.
The corpus describes what the live code produces.

**Not time-of-day sampling.** The stride-8 backfill was suspected of landing on
12 fixed times a day. It does not: all three corpora cover all 96 distinct close
times.

**Not the period.** Splitting the corpus at the same date shows the set scoring
+0.0900 [+0.0406, +0.1449] in exactly the last 8 days the replay calls weak.

What remains is the market POPULATION. The backfill only stores markets whose
reference series was served — 123 of 488 gold fetches failed, 183 of 490 on
silver — while the replay lost 40 of 500. If a failed fetch correlates with
anything about a market, the corpus is a survivorship-filtered sample and every
threshold fitted on it inherits that filter. This is not demonstrated, and it is
not dismissed.

**The consequence for the shipped configs.** Gold's edge is positive in every
view and its admitted group beats its blocked group in both. The MAGNITUDE is
uncertain: +0.09 from the corpus, +0.007 from the denser replay. Nothing here
should be quoted as gold earning +0.09 per contract live.

### 75b. Gold and silver keep New York hours (2026-09-25)

The operator's correction: gold and silver close at the New York close and
reopen at the New York open, so they are shut every weekend for about two days,
and the service must not poll through it.

Measured the same evening: both went quiet at 21:00 UTC, and Kalshi's next
listed market for either series opened **2026-09-27T22:00Z, 48 hours later**. At
a ten-second poll that is roughly 19,000 requests and an equal number of
reference fetches, recording nothing — the underlying metal is not trading
either.

**A closure is not the outage `market_gap_alert_s` exists to catch.** On
2026-09-24 Kalshi listed nothing for two hours while the service was healthy and
the operator's only symptom was Telegram going quiet. A false outage alert every
Friday night teaches the operator to ignore the one that matters, so the two are
told apart rather than merged.

**The first attempt was wrong, and it is worth recording why.** Inferring the
closure from the listing alone — "the next unopened market is far away" — looked
sufficient and is not. Checked against the live exchange, BTC and SOL *also*
reported their next unopened market 5.1 hours out, while both were trading
normally, because Kalshi creates markets in daily batches and an `unopened`
listing excludes the ones already open. That rule would have backed BTC off
during an outage and silenced its alert.

So the instrument **declares** that it observes sessions (`VENUE_HAS_SESSIONS`,
set on gold and silver only, off by default) and the **exchange supplies the
reopen time**. No hardcoded New York calendar, which would be wrong on every
market holiday, and nothing that can misfire on a 24/7 series. Four conditions
must all hold: the instrument declares sessions, no open market for 10 minutes,
Kalshi lists a next market, and it is at least 30 minutes away. An unknown
answer or a failed lookup is not a closure — the conservative direction is to
stay noisy.

While closed the service skips the reference fetch entirely and polls every ten
minutes rather than every ten seconds: a weekend falls from ~19,000 requests to
~320, and a reopen is still noticed within ten minutes. Sleeping the whole 48
hours was rejected — it would miss an early reopen, a config change and the
settlement sweep.

Twelve tests pin it, the load-bearing one being that a 24/7 instrument never
even asks.

### 75c. SOL trades live, against the measurement, by the operator's decision (2026-09-25)

Recorded with the evidence beside the decision, because that is the standing
rule when the operator decides against a measurement rather than with it.

**The measurement, which is not withdrawn.** SOL's UNGATED calibration residual
is **+0.0013 [-0.0124, +0.0159]** over 1,844 markets and 68 days: the market
prices these contracts correctly. Of 4,860 complete candidate gate sets, 1,073
cleared the 15% volume floor and the baseline and **not one** demonstrably beat
taking everything. The deployed set spans zero (+0.0222 [-0.0057, +0.0486]) and
its second disjoint half is negative. A 500-market replay of the most recent
days, through the real rule with the entry window applied, put the admitted
group at **-0.0375** against a blocked group at **-0.0157**. Two near-disjoint
samples, 57 and 68 days, agree there is nothing to gate for.

So this is not expected to make money on the evidence available, and every
figure above excludes fees per standing instruction — with no measured edge,
fees are the expected cost.

**What was enabled.** `AUTO_TRADE_ENABLED=true` in `run_sol.ps1`, plus the
stored `auto_trade_enabled` row written deliberately rather than left to the
.env default. Effective limits, read back from the service's own resolver:
$1.00 per order, one contract, **stops for the day at -$5.00**, at most 40
trades a day, 6 an hour, 120 seconds apart. Account cash at the time: $36.14.

**Why auto rather than approval.** There is no third option. Live orders need
either auto trading or a Telegram approval, and only ONE process may consume the
command stream: `getUpdates` is destructive, so a second poller silently steals
messages from the first, including the kill switch. BTC is that listener. Giving
SOL the command stream would have put BTC's `/auto off` in a race.

**The gap that had to be closed first.** That same design means `/auto off` can
never reach SOL, so enabling unattended trading would have left the daily loss
limit and `Stop-Process` as the only stops. `scripts/auto_switch.py` now writes
the same `settings` row `main.auto_is_on` reads on every decision — the stored
value already wins over the .env default, so it takes effect within one poll and
survives a restart:

    python scripts/auto_switch.py --db sol15.db --off

**A banner that asserted a safety property it never checked.** Startup printed
"execution requires Telegram approval" as a hardcoded string on every instance.
It became false the moment SOL was armed, and it is exactly the line an operator
would quote back. It now reads the real mode: the four approval instances say so,
and SOL says `AUTO TRADING ON, $1.00 per order, stops for the day at -$5.00`.

**The relabelled config.** `strategy_kalshi_sol.json` said "DATA CAPTURE ONLY -
automation OFF". Leaving that in place while real orders went out is the
stale-name failure this project keeps paying for, so the config now states that
it trades live by the operator's decision, against the finding, with the finding
still in it.

### 75d. A reboot took four of five instances down, and the banner had been lying (2026-09-25)

The machine rebooted at 19:06 local. **Only BTC came back.** `BTC15Signal` and
`BTC15Recorder` are the only boot-triggered scheduled tasks on the box, so ETH,
gold, silver and SOL stayed down with nothing to say so.

SOL had been armed for live trading about ninety minutes earlier. So *armed* and
*running* had come apart: the configuration said one thing and no process was
executing it. Nothing alerted, because the process that would alert is the one
that was down. That is the silent stop this system is most exposed to, arriving
by a route none of the existing guards cover — the watchdog restarts a service
that DIES, and a service that was never started does not die.

**The durable fix needs a privilege this session does not have.**
`scripts/install_instance_tasks.ps1` registers boot+logon tasks per instance,
mirroring `BTC15Signal` exactly (restart 999 at one-minute intervals, no
execution time limit, S4U, Highest) and running each instance's own launcher with
a new `-Supervise` switch so `watchdog.py` supervises it. The switch lives in the
existing launcher rather than a second script on purpose: a copied environment
block is how one instance ends up writing another's database. Registering it
fails with "Access is denied" from a non-elevated shell — even a plain logon-only
task — so it is the operator's one elevated command.
`scripts/install_startup_fallback.ps1` is the non-elevated alternative and fires
at logon rather than boot, which an unattended reboot at the lock screen would
miss.

### The banner had been asserting a safety property it never checked

Startup printed `execution requires Telegram approval` as a **hardcoded string**
on every instance. Section 75c replaced it with a read of
`auto_is_on(store, settings)`. When the four restarted instances came up on the
new code, they reported what was actually true:

| instance | reported before | actual, from the stored flag |
|---|---|---|
| BTC | "requires Telegram approval" | **AUTO ON** since 2026-09-20 22:58, day stop -$20 |
| ETH | "requires Telegram approval" | **AUTO ON** since 2026-09-24 13:36, day stop -$10 |
| GOLD | "requires Telegram approval" | no stored flag — approval, correct |
| SILVER | "requires Telegram approval" | no stored flag — approval, correct |
| SOL | — | **AUTO ON** since 2026-09-25 18:40, day stop -$5 |

BTC and ETH had been trading unattended for five days and one day respectively.
That is the operator's own `/auto on`, not a change made here — but every report
of the execution modes up to this point, including ones given to the operator,
was wrong, and the source was a string that looked like a fact.

**The lesson is the one this file keeps relearning in new costumes.** A banner,
a label or a config comment that states a property without reading it is worse
than silence, because it is the line someone quotes back. `auto_trade_enabled`
is a STORED row that outlives the .env and outlives a restart, by design, so
that `/auto off` from a phone cannot be undone by rebooting — which means the
.env default says nothing about what is armed. Read the row:

```sql
SELECT value, updated_at FROM settings WHERE key='auto_trade_enabled';
```

**And the kill switch does not reach four of the five.** `getUpdates` is
destructive, so only BTC consumes the command stream. `/auto off` cannot stop
ETH, gold, silver or SOL. `scripts/auto_switch.py` writes that same row for any
instance, which is why it had to exist before SOL was armed rather than after.

---

## 76. At-the-money, price action picks the side: no edge on any asset (2026-09-25)

Operator specification: on the 15-minute markets, use price action on the
underlying to choose UP or DOWN, and enter only when Kalshi offers that side at
45-50c. Measured by `scripts/measure_atm.py`: ~6,400 settled markets per asset,
69 days, first qualifying minute per market (elapsed 1-13), held to
settlement, net of `kalshi_fee_charged` at one contract, CI clustered by day.
24 price-action rules (1/3/5/15/60/240-minute returns, move since window open,
distance from strike, same-colour candle streaks, 15/60-minute range position,
EMA 5/20, Kalshi's own mid change), each scored as continuation AND reversal:
48 rules, Holm across the family.

| BTC, no information (controls) | n | win | ask | net/contract | 95% CI |
|---|---:|---:|---:|---:|---:|
| always UP | 3412 | 47.2% | 0.475 | -0.0212 | [-0.0373, -0.0052] |
| always DOWN | 3423 | 47.4% | 0.477 | -0.0198 | [-0.0353, -0.0029] |
| coin flip | 3481 | 46.5% | 0.476 | -0.0287 | [-0.0446, -0.0124] |

**The price is right at 50c, and the fee is at its maximum there.** A 47.5c
ask wins ~47%; the loss is the ~1.75c fee. To break even a rule must add ~2
points of win rate the price does not already carry.

**No rule does, on any asset.** Best of 48 on BTC: `r5>0 cont` +0.0003
[-0.0183, +0.0189], `window_move>0 cont` +0.0001, `streak>3 cont` +0.0383
[-0.0399, +0.1187] (n=273). Holm p = 1.000 for every rule.

**Replication kills the only candidate.** `streak>3 cont` (3+ same-colour 1m
candles, follow them): first half of days +0.0699 (56.7%, n=157), second half
**-0.0044** (49.1%, n=116); ETH **-0.0490**, SOL **-0.0462**. Best on ETH
(`window_move>3 cont` +0.0069) and SOL (`r60>20 rev` -0.0045) are different
rules, each spanning zero - the best-of-48 noise ceiling, not a signal.

**One consistent shape, too small to trade:** on BTC continuation beats
reversal for short horizons (r5: 49.5% vs 46.3%), i.e. at 50c the side the
underlying is moving toward is very slightly underpriced. It brings a rule to
breakeven, not past the fee, and it does not replicate on ETH or SOL.

Caveats: underlying is Binance spot 1m klines, not BRTI; fills assume the
candle-close ask was takeable for one contract (no historical depth).

**Decision: not built.** Consistent with sections 1, 14, 33 and 36: the only
durable bias here is favourite-longshot, and at 45-50c there is neither a
favourite nor a longshot - only the peak fee. What would change it: a signal
worth >2 points of win rate at 50c that replicates on disjoint days AND
another asset, on BRTI.

## 77. brti-4: the level lookback is 45 minutes, and what that invalidated (2026-09-25)

Written on the way to a commit, because a six-lens audit of the uncommitted diff
found that **the largest behavioural change in it was recorded nowhere here.**
`grep -c "brti-4" FINDINGS.md` returned 0, as did `brti-3`, `level_window` and
`19,305`. Every supporting measurement lived only in source comments, and the
three new strategy configs referred to "under brti-4" without anything in the
durable record saying what brti-4 is. That is the failure this file exists to
prevent, so it is fixed here.

### What changed

`features_from_series` gained `level_window_s`, and it went from the market's own
15-minute window to **2,700 seconds** - the running window plus the 30-45 minutes
before it. Three features are computed from `level_pts` and therefore all three
changed meaning: `brti_rejections`, `brti_held_s` and `brti_accel`.

The feature contract moved `brti-2` (`9e41a3dfea0f7059`) → `brti-3`
(`60c45b5c38c8ff5d`) → `brti-4` (`a641ab8e2a05aa44`), and
`SETUP_FEATURE_VERSION` in `adaptive.py` is now the single definition that
`feature_contract.py` reads - reversing the import to remove the second version
literal that had already caused one silent failure (a stale `brti-2` there made
`learning_data` discard every row, so `live_actual_fills` read 0).

### Why the rejection count had been measuring the opposite of its name

At 900s the lookback WAS the market's own window, and the strike IS that window's
opening price - so every window began with price sitting on the strike, inside
the rejection threshold. A clean one-way move therefore scored exactly **1**
rejection: its own departure. Reaching 2 required the move to have wobbled back
toward the strike.

The deployed gate asked for `>= 2`, so it refused the cleanest setups. Over
19,305 brti-2 corpus points the refused bucket led on every measure:

    distance   8.33  against  4.58
    held        241s against   212s
    momentum     6.3 against    3.2
    win rate   75.6% against  66.9%

with `corr(rejections, distance) = -0.319`. Widening to 2,700s moves the median
rejection count from 1 to 2, so the same threshold now asks the question its name
implies: did price approach this level and get turned back.

This is also the operator's correction that started it - a clear winning setup
was refused, and "how far back do we look" turned out to be the whole defect.

### What it invalidated, and what is still deployed on the old meaning

Every threshold fitted under brti-2 describes a quantity that no longer exists.
`feature_contract.py` states this and the five live policies refuse to load and
refit, which is the intended behaviour.

**BTC and ETH still carry brti-2-era level thresholds.** Both configs were
untouched by the 2026-09-25 work and still deploy:

    "min_brti_accel": -5.0
    "min_brti_held_s": 120.0
    "min_brti_rejections": 2

Those gates now read the 45-minute quantity. Their live admission behaviour
therefore changed the moment `brti.py` shipped, without either config or this
file recording it. On 5,547 brti-2 points the same three gates measured: the
rejection gate costs 69% of volume for nothing, `accel >= -5` refuses its own
better bucket, and `held >= 120s` binds on 9 of 5,547. Re-measuring them on a
brti-4 BTC corpus and deciding whether to keep, widen or retire them is an OPEN
ITEM and the operator's call; nothing here changes BTC or ETH.

### Two entry windows, and only their intersection runs

Also found by the audit. Entry is gated twice and independently:

    settings.entry_from_seconds / entry_to_seconds   660 / 360, the defaults,
        which NO launcher or .env overrides - checked in main.py before the rule
        is consulted at all;
    the rule's own entry_from_seconds / entry_to_seconds, from the strategy JSON
        - checked again in kalshi_signal.py and kalshi_brti.py.

So SOL's config said 800-250s while the service could only ever act between 660s
and 360s, and gold's and silver's said 700-400s.

**The fits are unaffected and behaviour is unchanged.** The corpora hold decision
points at 360, 420, 480, 540, 600 and 660 seconds only, so gold's and silver's
700-400 selected exactly `{420..660}` - which is what live ran - and SOL's
800-250 selected exactly `{360..660}`, likewise. The three configs are narrowed
to 660-420, 660-420 and 660-360: the same decision minutes, now stated honestly.

What that removes is a trap rather than a bug. Had the Settings window later been
widened, SOL would have begun trading at 780, 720, 300 and 250 seconds - none of
which any corpus point covers, so none of which was ever measured. It also means
the window rungs in `fit_instrument_config.py`'s ladder were never
distinguishable on this data, and the script's comment claiming a 780..120s grid
was wrong.

### Measuring it again exposed a stale denominator

With the window applied, the guard test's base is the points the rule can
actually take. Gold and silver had been scored against a base that included the
360-second rows their window excludes:

    GOLD    318/1745 = 18.2%  244W-74L  76.7%  residual +0.0728
    SILVER  233/1485 = 15.7%  185W-48L  79.4%  residual +0.0673
    SOL    1308/6396 = 20.5%  962W-346L 73.5%  residual +0.0173

SOL is unchanged to the contract, as predicted, because its window includes 360.

### 77a. A six-lens audit of the uncommitted diff, and what it found (2026-09-25)

Before pushing 67 changed files touching live trading code, six independent
review passes ran over the working tree — secrets, live-behaviour risk, test
integrity, knowledge capture, internal consistency, loose ends — each reporting
only defects it could point at in the content. **59 findings: 6 blocker, 26
important, 27 minor.** The adversarial verification stage was stopped after the
audit phase: the material findings were verified first-hand instead, and ~120
verifier agents on prose inconsistencies was not a good trade. So the triage
below is mine, not a panel's, and is marked as such.

The audit earned its keep twice over. It found a defect introduced by the very
feature that was meant to prevent false alarms, and it found that the session's
largest behavioural change was undocumented (now FINDINGS 77).

### Fixed before the commit

**The weekend closure ended in a false outage alert.** `venue_closed_gap_s`
decides whether a gap IS a closure; it was also applied on every later poll, so
once the reopen came within 30 minutes the closure lapsed and the outage path
reported the whole 48-hour weekend as "NO MARKET AT THE EXCHANGE". That would
have fired this Sunday on both metals — the exact alert-fatigue the feature was
built to avoid. A closure now ends when the market opens, and past the expected
reopen with no market it becomes a genuine outage again. Two tests pin both ends.

**A test opened the LIVE gold database read-write.** `Store(ROOT / "gold15.db")`
runs the store's schema DDL, so running the suite mutated a production trading
database of a running service — and the suite was run many times on 2026-09-25
with all five instances up. It also asserted `settled > 0` on live rows, so its
verdict depended on how trading was going, and it passed by `return` whenever the
file was absent. Rewritten against a temporary database with rows from two
series, which pins the same scoping property and fails if scoping is removed. No
other test touches a live database.

**The banner could announce AUTO TRADING ON when execution was impossible.** It
read the stored flag alone — the same unchecked-claim shape it had just replaced,
in the other direction. It now names what is missing instead.

**Two entry windows, only the intersection running** (FINDINGS 77), plus a guard
that no config may advertise a window wider than the service's.

**Per-instance credential files were ignored by name, not by pattern.**
`.gitignore` listed `.env.eth` individually, so `.env.gold`, `.env.silver` and
`.env.sol` were uncovered — the same shape of gap that leaked the credentials
originally. Now `.env.*` with `!.env.example`.

**Three stale statements** repaired: the test-config docstring still said SOL ran
with automation off, the gold test quoted the superseded fit and asserted a band
"entirely BELOW" BTC's when 0.60-0.80 overlaps 0.70-0.93, and the frozen contract
named brti-3 while being brti-4.

### Open, recorded rather than fixed

**The shared-account exposure scope is inconsistent, and it touches a capital
guard.** Three findings converge here: `_scoped_open` scopes by "is this a
recognised instrument" rather than by this instance's series, so on one account
each instance still counts the other four; `open_mark_detail` is still written
from the unfiltered `per_ticker`, so the operator-facing "Open position" figure
remains account-wide; and narrowing the `open_mark` row loosens the recovery
add's held-exposure guard, which reads it. FINDINGS 70 said that row must stay
account-wide. This is real money and a guard rather than a display, so it is not
being changed in the same breath as a push — it is the next thing to do.

**A transient fetch failure can lose a settlement margin permanently.** A failed
settlement-facts fetch still marks the row as fetched with NULL strike and value,
so that market's margin is unrecoverable, and the comment claims the flag means
Kalshi published neither number.

**"Intelligence NOT acting" will appear on every gold, silver and SOL alert** in
their documented normal state — a line written as the exception becoming
permanent furniture, which is the message-clarity failure the operator has
objected to before.

**`strategy_version` hashes `strategy.json`, not the per-instrument config**, so
all five instruments share one digest and the refit of three configs does not
change what stamps their decisions.

**The metals launchers are gold's launcher copied**, so silver's and SOL's
headers assert gold's measured residual and an exposure arithmetic that does not
match what they set — on the one instance trading with no edge at all. The
five-instance aggregate daily floor is $47 and is recorded nowhere.

**Smaller, all confirmed:** the closure comment says 48 hours where the setting
and its test say 53; FINDINGS 75c says four instances are in approval mode where
75d's table shows two; `check_recent_window.py` cannot run for BTC;
`measure_corpus_flow.py` hardcodes the project root; `fetch_trades.py` still
defaults to the 1 GB truncated corpus that produced withdrawn finding 67; the
four new watchdogs share one bot token and the watchdog's alert names no
instance, so a give-up alert cannot be attributed; `tune_*.py` remain superseded
but are now marked.

**What the audit cleared.** `gate_binds` is decision-neutral — a gate whose
threshold nothing can fail always passes, so excluding it from the count cannot
change a qualification, verified independently against every caller. All five
configs load with no unknown key. No secret is in the commit.

## 78. The loss step waits for a 0.70-0.79 ask (2026-09-25)

Operator instruction, shipped as instructed: "the recovery after a loss does not
need to be triggered automatically, as we will make it wait for the best
opportunity ... around the 70 to 79 range ... meaning recovery can happen 3 to 5
trades later when the best opportunity presents itself, so that we are not making
20 cent profit on a recovery trade where normal sizing can offer the same on a
better opportunity."

### What changed

The upsize no longer fires on whatever trade comes next after a loss. It arms,
waits for an ask inside 0.70-0.79, and fires there — which may be several markets
later. Three bounds stop a wait becoming a standing upsize:

* it expires unspent after `loss_step_wait_markets` (5) settled markets;
* it fires ONCE per losing episode, and "already spent" is READ off
  `trade_proposals` rather than trusted to a flag, so an order that failed after
  a flag was written cannot hide it;
* the budget never escalates — a second loss re-arms at the same $2.

The arming loss and the count of markets since it both come from
`Store.settled_bot_markets`, which is the same join, fee handling and early-exit
arithmetic as `last_market_lost`. That is deliberate: a second, slightly
different notion of "the bot lost" beside the reconciled one is how the
2026-09-24 defect happened, where the operator's own 10-contract manual fills in
the same market moved a figure meant to describe the bot.

### The payoff reasoning is right, and it is not the whole calculation

The operator's arithmetic is exact: the extra contract wins `1 - ask` and loses
`ask`, so at 0.90 it risks 90c to make 10c and at 0.75 it risks 75c to make 25c.
Spending the step at the top of the band earns about 20c, which base size would
have earned anyway at a better price.

What that reasoning leaves out is that expected value per extra contract is

    p * (1 - ask) - (1 - p) * ask  =  p - ask

which is the calibration residual itself. **The payoff ratio cancels.** So "where
does an extra contract pay most" and "where is the market most mispriced" are the
same question, and 25c at 0.75 beats 10c at 0.90 only if the win rate fails to
make up the difference. On BTC it does not. Measured on 7,139 priced brti-4
decision points over 64 days, ungated:

| band | win% | per contract | 95% CI | per dollar |
|---|---|---|---|---|
| 0.70-0.75 | 73.1% | +0.0104 | [-0.0253, +0.0446] | +0.0138 |
| 0.75-0.80 | 76.5% | -0.0053 | [-0.0518, +0.0407] | -0.0067 |
| 0.80-0.85 | 81.4% | -0.0051 | [-0.0413, +0.0312] | -0.0063 |
| 0.85-0.90 | 89.4% | +0.0242 | [-0.0076, +0.0533] | +0.0284 |
| 0.90-0.93 | 94.1% | **+0.0240** | **[+0.0009, +0.0437]** | +0.0261 |

    in 0.70-0.79   +0.0026 per contract, 21.7% of setups
    outside        +0.0159 [+0.0004, +0.0315]

On that population the chosen band is where the extra contract earns **least**,
and the only cell whose interval clears zero is 0.90-0.93 — the one the
instruction singles out as not worth taking.

### Why that table is suggestive and not decisive

It is UNGATED. The step only ever sizes a trade that has already passed every
gate; it never causes one. The population that decides the question is therefore
the qualifying subset, and the gates select on distance, momentum and level
behaviour, all of which correlate with price. That measurement could not be made:
BTC's deployed floors are brti-2-era and admit too few brti-4 points to score
(FINDINGS 77 records that those thresholds are themselves now measuring a
different quantity). So the honest position is that the band is unmeasured where
it matters and contradicted where it could be measured.

The operator decided with that in view and instructed it be shipped. Sizing is
theirs, and it is implemented in full, with the interval above recorded so the
decision can be revisited against live results rather than re-argued.

### What it also fixed

Two defects found while implementing, both introduced by the change itself and
caught before shipping.

**An early cut returned base size whenever the last market had won**, which
killed the feature outright: the win rate is about 3 in 4, so the market after a
loss usually wins, and the armed step has to survive exactly that. Every other
test still passed. `test_it_survives_a_win_and_fires_later` now pins it.

**The add-on stood down on the wrong condition.** It keyed on "did the last
market lose", which was the same thing while the step fired immediately. Now the
step usually waits, so that would have stood the add-on down for an upsize that
never happened — removing one mechanism without engaging the other. It keys on
the step actually firing.

Both RECOVERY ARMED messages said "the next entry is sized to $X; a win resets
it". Neither half is true any more, and they now state the band, the wait and
that a win no longer resets it.

## 79. XRP backtested and shipped as a shadow: no edge, and the first clean corpus (2026-09-26)

Operator instruction: backtest XRP and implement it in the shadow. Both done.
`KXXRP15M` exists with the same shape as the rest - `floor_strike` present,
`/live_data/events/...` serving the per-second reference - so the whole pipeline
applied unchanged.

### What was built

    4,262 settled markets      2026-08-12 .. 2026-09-26, 45 days, 67,992 candles
    6,390 decision points      1,065 markets, stride 4
    5,713 carry brti_retrace   89% of them

**XRP is the first instrument fitted with all fourteen live gates visible.**
`brti_retrace` and `brti_choppiness` were computed by
`features_from_series` and then discarded by the backfill until 2026-09-25, so
every earlier fit - BTC, ETH, gold, silver, SOL - scored candidate sets as though
those two gates were absent while live enforced them (FINDINGS 75). XRP's corpus
was built after that repair, so nothing here is fitted blind to a live gate.

### The backtest says there is no edge

    UNGATED   n=6,335   win 75.3%   mean ask 0.740   residual +0.0130 [-0.0064, +0.0324]

That interval spans zero: over 46 days the market prices these contracts about
right. Of **19,440 complete candidate gate sets**, 673 cleared the 15% volume
floor AND the ungated baseline, and **not one reached tier A or tier B** - none
demonstrably beat taking everything. The best:

    price 0.55-0.90, gap 0-25bp, mom<=40, |accel|<=25, held>=30s, rej>=2,
    window 660-420s, retrace <=0.60 AND required

    n=1,458 (23.0% of points), 664 markets, win 76.7% at 0.741
    residual      +0.0257 [-0.0029, +0.0537]   spans zero
    vs ungated    +0.0127 [-0.0083, +0.0350]   does not show
    first half    +0.0102 spans zero
    second half   +0.0390 spans zero

This is SOL's shape, not gold's: SOL ungated +0.0013, gold +0.0346. Under the
real rule in-window it admits 27.4% - 1118W-340L, 76.7% at a 0.741 ask - against
a blocked group winning 73.8%. Better than what it refuses, and not by enough to
call an edge.

Marginally nothing rescues it. No price bucket clears zero (0.55-0.65 reads
-0.0004, 0.65-0.75 +0.0191 spanning zero), and the search was offered six
narrower distance bands including gold's 1.5-6.0 and silver's 0.5-12.0 and
preferred **none** of them - distance does not separate on XRP.

### One genuinely new thing: the fit asked for retrace

XRP's search chose `max_brti_retrace: 0.60` **and**
`require_measurable_retrace: true`. Gold, silver and SOL all preferred `false`,
on the reasoning that a window with no advance to give back is the target state
on a level-maintenance instrument. XRP wanted the opposite, at a cost of about
11% of setups. Recorded as measured, not as understood - it is the first
instrument that could express the preference at all, which is itself the argument
for having fixed the corpus.

### Shipped as a shadow, and what that means here

Automation is OFF and the launcher says why in the file rather than in a
changelog. It is not in `MIRROR_INSTANCES`, so it copies nothing to the mirror
account. The instance exists to RECORD: live per-second reference data and a gate
set admitting 23% of decision points, which no backtest can reconstruct because
Kalshi serves no historical order-book depth - fill probability and queue
position are only ever answerable from data this system archives itself.

Arming it later takes two deliberate acts, `AUTO_TRADE_ENABLED=true` in the
launcher and `auto_switch.py --db xrp15.db --on`, because the stored row wins
over the .env default so a restart cannot arm it by itself.

### Two defects from generating the launcher by copy

Recorded because the audit had already flagged this exact failure for silver and
SOL - both are run_gold.ps1 with names swapped, so both assert gold's measured
residual for instruments their own configs deny. Copying SOL's launcher for XRP
reproduced it and added two live ones:

* **`AUTO_TRADE_ENABLED=true`** came across from SOL. XRP would have started
  trading unattended on an instrument with no measured edge, exactly against the
  instruction to ship it in the shadow.
* **`CORPUS_MARKET_PATH`** became `market_data_kxxrp15m_new.db`, a file that does
  not exist, because SOL's line names a `_new` database.

Both fixed, and the prose rewritten to describe XRP. A launcher generated by
substitution carries the previous instrument's evidence, and evidence attached to
the wrong instrument is worse than none.

## 80. Kalshi does not price combos at the product, and the venue decides the edge

Every combo script here was built on one premise: that the exchange prices a
multi-leg combo as the PRODUCT of its legs - the independence assumption - so a
same-direction combo on correlated assets is structurally underpriced. That
premise is FALSE for the quoted path, and measuring it was only possible because
the operator sent screenshots of the app's own buy ticket.

Read off those screens, and cross-checked against live RFQ quotes:

    legs                product   copula   app price   vs product   vs copula
    0.63 0.62 0.74 up    0.2890   0.4732     0.5426       1.88x        1.15x
    0.60 0.55 0.68 up    0.2244   0.4132     0.5435       2.42x        1.32x
    0.61 0.62 0.82 up    0.3101   0.4798     0.5435       1.75x        1.13x

The app charges 1.75-2.42x the product. Kalshi ALREADY prices the correlation
and adds 13-32% on top. There is no independence mispricing to harvest there.

BUT THE ORDERBOOK IS A DIFFERENT VENUE AND PRICES NEAR THE PRODUCT. A resting
bid on a combo's own book filled at 0.0500 where the product was 0.0456 and the
copula said 0.1219 - 41% of fair value, against the app's 113-132%. Same
instrument, same minute, opposite side of the edge. WHERE you buy decides
whether a combo is cheap or dear. That is the finding.

The 40,578-point correlation measurement is untouched by this: it is a statement
about the ASSETS (all five settle alike 45.4% where the product implies 19.3%),
not about Kalshi's pricing. What was wrong was the inference from it to a
tradeable edge on the quoted path.

## 81. The combo RFQ API works; three details make it 404, and one makes it lie

The documented flow (POST /communications/rfqs, quotes over the authenticated
websocket, accept, verify fills) is real and reachable with our key. It 404s
until all three of these are right:

  * the combo MARKET must be created first, via
    POST /multivariate_event_collections/KXMVECROSSCATEGORY-R, and its returned
    `market_ticker` passed in the RFQ body. Without it: 404 not_found.
  * the sizing field must sit at the TOP level. Nested inside an `rfq` wrapper
    the server replies "Either contracts/contracts_fp or target_cost_dollars
    must be provided" while ignoring the one you sent.
  * a second RFQ on the same combo market returns 409 already_exists.

Quotes come back on GET /communications/quotes?rfq_creator_user_id=<own id>;
the id is on any row of /portfolio/orders. REST polling reaches the same rows as
the websocket and needs no new dependency in a live money process.

AND THE PRICE FIELD LIES IF READ NAIVELY. A quote carries `yes_bid_dollars` and
`no_bid_dollars`: they are the maker's BIDS, what they would pay US. Buying
costs `1 - no_bid`. Reading yes_bid as the purchase price made every combo shape
look like it cost 1.5-2.2 cents. Checked arithmetically against a real fill on
this account: quote no_bid 0.4750 -> 0.5250 a contract, 1.84 contracts filled
for $0.97 = 0.527 each.

ACCEPTANCE IS NOT A FILL. Of 8 accepted quotes on this account 6 confirmed and 2
went to status `cancelled` - the maker has ~3 seconds. Read the fill, not the
acceptance.

## 82. Makers ignore most combo RFQs, and the shape of the ask decides it

415 RFQs on this account, 10 ever quoted - 2.4%. The pattern is not random:

    sized by contracts 4.44    3/5   60% quoted      2 legs    0/137   0%
    sized by contracts 1.84    3/6   50%             3 legs    8/157   5%
    sized by contracts 1111    0/37   0%             4-9 legs  0/30    0%
    target_cost_dollars $2     0/5    0%
    target_cost_dollars $5     0/1    0%
    target_cost_dollars $11    0/2    0%

A `target_cost_dollars` RFQ - the dollar box in the app - has never once been
quoted here, and neither has a two-leg combo. Ask by small contract count, on
three legs.

## 83. The combo markup is not uniform, so every quote needs pricing

One window, BTC+ETH+ZEC, all eight shapes quoted at once, quote against the
fitted copula:

    shape                    product   copula    payout   quoted   quote/fair
    ALL UP                    0.7795   0.8397      1.3x   0.8720      1.04x
    one opposite (ETH up)     0.0788   0.0316     12.7x   0.0320      1.01x
    one opposite (BTC up)     0.0124   0.0236     80.7x   0.0260      1.10x
    one opposite (BTC down)   0.0409   0.0126     24.4x   0.0610      4.83x
    one opposite (ETH down)   0.0064   0.0105    155.3x   0.0530      5.03x
    ALL DOWN                  0.0007   0.0187   1536.1x   0.0390      2.09x

Same window, same legs: some quotes come back at 1.01-1.10x fair value and
others at 3.6-5.0x. So the trade is never "buy this shape" - it is "buy this
shape only when the quote prices near fair", which requires the model on every
quote. combo_rfq.py refuses anything that fails --min-edge.

## 84. A combo's return comes from the co-move, not from the payout ratio

The operator's $1.00 all-up BTC+ETH+ZEC combo, bought when the legs were
60/55/68, was worth $1.77 nine minutes later at 99.7/99.1/99 - up 78%, against a
max payout of only 1.84x. The profit was not leverage in the payout sense; the
combo simply repriced from 0.5435 to ~0.96 of max as the legs converged.

Because a combo is a PRODUCT of its legs, a modest co-move in three of them is a
large percentage move in the combo. That is the amplifier, and it is the one
this system is actually equipped to exploit, since what it predicts is direction
in the closing minutes.

It also puts the markup in proportion: that entry was 1.32x fair value and still
returned 78%, because a correct directional read on three correlated legs swamps
a 32% entry cost. The markup is a drag, not a disqualifier - and worth avoiding
via the orderbook where the trade allows it.

Leverage in the PAYOUT sense does need an opposite leg, as the operator said:
all-up pays 1.3x, one-opposite pays 8-241x. Those are different trades. The
all-up trade wants a directional call and an early exit; the one-opposite trade
wants a cheap quote and is a lottery ticket.

## 85. Live combo record so far: six settled, six losses, and why that is not the whole story

Read from the broker, not reconstructed: five combos executed through the RFQ
path on this account and one bought by resting a bid, all settled for $0 against
roughly $4.67 staked. That record is real and is the reason none of the above is
being wired into an automated strategy yet.

But the six are not one experiment. The five RFQ fills were bought at 1.13-1.32x
fair value and held to expiry - structurally losing trades. The resting-bid fill
was bought at 0.41x fair value on a 12% event that did not happen - a correctly
priced trade that lost, which is what most correctly priced 12% trades do. And
the operator's own all-up combo, cashed out rather than held, returned +78%.

Entry price, venue and exit discipline separate these, and a six-trade tally
that ignores all three teaches nothing. What it does justify: no automation
until a measured sample exists, sized at $1, with every entry priced against the
model and the exit taken on the move rather than at expiry.

## 86. Kalshi's combo markup is real: it prices 3-leg baskets at rho 0.92 where reality is 0.69

Section 80 established that the exchange does not price a combo at the product of
its legs. Backing a one-factor copula out of its prices says WHAT it does charge:

    3-leg baskets    implied rho  0.9176
    8-leg baskets    implied rho  0.7155
    fitted from settlement history          0.6944

Two readings fit those numbers equally well from prices alone. Either Kalshi
overcharges correlation on small baskets, or OUR rho is too low and the apparent
markup is our own model error showing up wherever rho has the most leverage -
which is exactly at few legs and mid-range probabilities. Prices cannot separate
them. Outcomes can.

THE TEST (scripts/test_which_rho.py). For all 40,578 (window, minute) points
where BTC, ETH, SOL, XRP and NEAR were quoted at once, take each asset's own
quoted probability, form all 26 subsets of size 2-5, predict P(all settle up) at
a range of rho, and score against what happened. 1,055,028 predictions per rho.
Scored by calibration error and by log-likelihood, which is a proper scoring rule
and so cannot be gamed by a model that gets the average right and every case
wrong.

         rho      k=2      k=3      k=4      k=5   overall       logLik
      0.6944   0.0122   0.0126   0.0097   0.0062    0.0102      -502494  ours
      0.7500   0.0112   0.0158   0.0182   0.0213    0.0166      -503151
      0.8500   0.0244   0.0372   0.0456   0.0514    0.0397      -506744
      0.9176   0.0362   0.0551   0.0670   0.0752    0.0584      -511583  Kalshi

rho = 0.6944 wins on both criteria, by 9,089 nats of log-likelihood over 0.9176.

WHY THIS IS NOT CIRCULAR. The original rho was fitted to the FIVE-asset all-agree
frequency and nothing else. The k=2, k=3 and k=4 columns are out of sample, and
0.6944 wins every one of them. It is also scored at real mid-window quoted
prices, which is where a combo is actually bought, not at the ~50% marginals of a
window's open.

WHAT IT MEANS FOR MONEY, measured on today's three entry styles:

    style                legs    fair    paid   paid/fair   EV hold
    early 3-leg ~60%        3  0.4132  0.5435      1.32x    -24.0%
    late  6-leg >=85%       6  0.7470  0.7795      1.04x     -4.2%
    early 8-leg ~80%        8  0.5104  0.5025      0.98x     +1.6%

The firm, actionable result is the NEGATIVE one: a 3-leg combo bought at
mid-range leg probabilities carries a ~30% markup and is a losing trade before
anything else happens. The +1.6% on the 8-leg is inside noise and carries brutal
variance - on the very window this was measured, NEAR went 82% to 9% and turned a
$1.99 payout into a 17c expectation. No shape tested shows a reliable edge.

## 87. RETRACTION of #86: the leg-count edge is refuted, and the rho it rested on was wrong

Section 86 claimed Kalshi prices 3-leg combos at rho 0.92 where reality is 0.69,
making them a ~30% markup, and that larger baskets are priced near fair. An
adversarial review and two follow-up measurements killed it. Both defects are in
OUR work, not the exchange's.

DEFECT 1 - THE SAMPLE WAS COUNTED ELEVEN TIMES. The "40,578 aligned points" are
3,788 distinct WINDOWS contributing ~11 rows each, at minutes 1..14 before the
same close. Every row in a window shares ONE settlement outcome, so they are not
independent observations. The 9,089-nat log-likelihood gap quoted in #86 is
inflated about 11x; honestly it is ~800 nats. That still separates a pooled rho,
but it is an order of magnitude less evidence than was claimed.

DEFECT 2 - THE POOLED RHO IS WRONG, AND A POOLED RHO IS THE WRONG OBJECT. Fitted
by maximum likelihood on the 3,788 INDEPENDENT windows, the all-5 rho is 0.8070,
not the 0.6944 obtained by matching an all-agree frequency on the over-counted
rows. And the pairwise values are nothing like equicorrelated:

    SOL+XRP  0.9700     BTC+XRP  0.6986     SOL+NEAR  0.4171
    BTC+SOL  0.7862     ETH+SOL  0.6509     XRP+NEAR  0.3424
    BTC+ETH  0.7576                         ETH+NEAR  0.3044
    ETH+XRP  0.7471                         BTC+NEAR  0.2582

A spread of 0.71, which is 3.6x the entire 0.197 leg-count effect. A one-factor
rho IS the average pairwise correlation, so walking a basket from 3 majors to 8
names including weak alts can only DILUTE it. The leg-count gradient in implied
rho is what an equicorrelated estimator produces when inverted against a
heterogeneous truth - it appears even when every basket is priced exactly fairly.

RE-MARKED AT THE CORRECTED RHO, the trade disappears:

    basket                paid   fair@.6944  ratio   fair@.8070  ratio
    3-leg BTC+ETH+ZEC   0.5435      0.4132   1.32x       0.4522   1.20x
    3-leg 11:19 picks   0.5426      0.4732   1.15x       0.5108   1.06x
    6-leg late >=85%    0.7795      0.7470   1.04x       0.7788   1.00x
    8-leg all-up        0.5025      0.5104   0.98x       0.5714   0.88x

The 8-leg lands at 0.88x fair - the exchange selling below fair value, which it
does not do. That impossibility is the tell: the model, not the venue, is wrong.

WHAT ACTUALLY MOVES THE EDGE, all measured without a copula:

  * VENUE, about 3x. The combo's own orderbook filled at 0.41x fair in the same
    minute the app path charged 1.13-1.32x (#80). Nothing else comes close.
  * PER-QUOTE VARIANCE, about 5x. Same window, same three assets, all eight
    shapes quoted at once: quote/fair ran 1.01x to 5.03x (#83). That swamps any
    leg-count term by 25x.
  * EXECUTABILITY. 4-9 leg combos have been quoted 0 of 30 times by RFQ (#82), so
    "buy more legs" is only available on the app - the expensive venue.

Leg count allegedly moves it 1.1x. It is not the variable.

WHAT WOULD SETTLE IT, if it is ever worth revisiting: a NESTED ladder. In each of
>=30 independent windows, at ONE timestamp, quote {A,B}, {A,B,C}, {A,B,C,D},
{A,B,C,D,E} built from the SAME ordered legs drawn only from corpus-covered
assets, recording each leg's individual ask (not just the product) and both sides
of the combo quote. Nesting is the only design in which leg count is the only
thing that changes, so composition and price level can no longer travel with it.
Paired sign test within window. Anything less repeats this mistake.

METHOD NOTE FOR NEXT TIME. The error that produced #86 was scoring a model on
rows rather than on independent events, then reading a large likelihood gap as
strong evidence. Count the events before quoting the evidence.

## 88. Our own signals, taken as combos: the edge multiplies 3-6x, and the venue decides whether you keep it

Every earlier combo test here priced arbitrary baskets - all-up, one-opposite,
eight legs of whatever the app happened to show - and each was a bet on
correlation alone. But this system does not sell correlation. It sells a measured
entry edge on setups its gates admit. The operator's instruction was to use the
exact signal, from trigger to settlement, and treat simultaneous ones as a combo.

THE SIGNAL IS NOT REIMPLEMENTED. scripts/signal_combo.py builds the real
`KalshiBRTIRule` from each deployed strategy_kalshi_*.json and calls the live
`check_facts`, then applies the same entry-window test as
`kalshi_signal.evaluate`. One signal per window per asset - the FIRST qualifying
decision point, which is what the live loop takes. Counting later points in the
same window is the row-counting error that produced the retracted #86.

    asset  signals  days   win    mean ask   residual
    BTC        993    68  85.3%      0.826    +0.0271   *
    ETH       3320    68  84.2%      0.831    +0.0111   *
    SOL       1096    68  71.5%      0.688    +0.0277
    XRP        664    46  72.7%      0.713    +0.0140
    NEAR       483    46  69.8%      0.663    +0.0346

    * RETRACE GATE HELD OPEN. The BTC and ETH corpora have no `brti_retrace`
      column at all, and both configs leave `require_measurable_retrace` unset
      so the dataclass default True applies: the live rule demands a field the
      archive never stored, and every archived row is correctly refused. Those
      two are therefore NOT the deployed rule and are labelled everywhere.

COMPARED ON RETURN PER DOLLAR STAKED, which is the only fair comparison - a
combo contract costs the PRODUCT of the legs' asks while one contract of each
leg costs the SUM, so a per-contract comparison flatters the combo for being
cheaper. Paired within the same windows, day-clustered bootstrap, and
Holm-Bonferroni at FWER 0.05 across all 16 baskets:

    basket           n   all-win   cost  combo ROI  singles     diff        b/e  ret/risk
    BTC+ETH+XRP     87     77.0%  0.509     +52.6%    +8.1%   +44.5%      1.51x  0.60/0.22  HOLM
    BTC+ETH+SOL    142     66.9%  0.473     +41.6%    +4.7%   +36.9%      1.41x  0.40/0.12  HOLM
    ETH+SOL        544     71.0%  0.571     +24.9%    +6.7%   +18.2%      1.24x  0.30/0.15  HOLM
    BTC+XRP        112     73.2%  0.601     +20.5%    +6.8%   +13.7%      1.22x  0.27/0.18  HOLM
    ETH+XRP        327     71.6%  0.605     +18.3%    +4.9%   +13.5%      1.18x  0.24/0.12  HOLM
    BTC+ETH        714     79.6%  0.683     +17.2%    +4.1%   +13.1%      1.17x  0.28/0.11  HOLM

Six of sixteen survive Holm. The combo multiplies return per dollar by 3-6x, and
- the part that matters - RETURN PER UNIT OF RISK improves too, in every
surviving basket. So this is not merely leverage buying variance.

AND THE VENUE DECIDES WHETHER ANY OF IT IS KEPT. `b/e` is the most you can pay,
as a multiple of the product, before the edge is gone: 1.17-1.24x on two legs,
1.37-1.51x on three. Against that:

  * the combo ORDERBOOK quotes near the product. A resting bid filled at 0.0500
    against a product of 0.0456 - 1.10x - which is inside break-even for every
    surviving basket.
  * the app/RFQ path charges 1.75-2.42x the product (#80, #87). That is ABOVE
    break-even for all six. Bought there, every one of these is a losing trade
    even with the signal edge intact.

So the strategy is: wait for two or three deployed signals in the SAME window,
build the combo on exactly those legs, and rest a bid at no more than ~1.15x the
product for two legs or ~1.35x for three. Never take the app quote.

WHAT IS NOT ESTABLISHED. Five of the six surviving baskets contain BTC or ETH,
whose rule was relaxed to run at all. The only baskets using purely deployed
rules - SOL+XRP, NEAR+SOL, and the SOL/XRP/NEAR triples - did NOT survive. Until
BTC and ETH archive `brti_retrace`, this is a strong hypothesis on a relaxed
rule, not a measured result on the shipped one. The fix is to start recording
retrace for BTC and ETH and re-run in a few weeks, which costs nothing.

Co-occurrence is also rarer than the single-leg rate suggests: BTC+ETH fires
together ~10 times a day, the three-leg baskets 1-2. Sizing has to account for a
combo appearing in a minority of windows.

## 89. The alerts we actually SENT, taken as combos - and why only one basket can be tested

Section 88 answered the combo question by REPLAYING the deployed rule over the
feature archive. The operator's correction was exact: use the signals that were
already generated, the same ones that arrive on Telegram. That is a different
and better question, and it is what `scripts/alert_combo.py` reads.

WHERE THE REAL SIGNAL LIVES. Each instance's store carries an `observations` row
per poll, and `alerted = 1` marks the window where the Telegram alert went out.
The alert fires ONCE per window, at the first qualifying poll, and main.py
records the snapshot at alert time deliberately - "so the stored contract price
is the one the alert actually quoted". The first alerted row in a window IS the
signal that reached the phone.

THE ALERTS, as sent:

    asset  alerts  days   win    mean ask   residual
    BTC       492     6  76.0%      0.724    +0.0361
    ETH       197     3  74.1%      0.737    +0.0039
    SOL       106     2  75.5%      0.743    +0.0120
    XRP        27     1  74.1%      0.743    -0.0019
    NEAR       16     1  50.0%      0.745    -0.2450
    GOLD       72     1  76.4%      0.729    +0.0352
    SILVER     22     1  77.3%      0.734    +0.0383

These win rates are LOWER than the replay's 82-85% and the asks are lower too,
and that is correct rather than a discrepancy: the alert fires at the first
qualifying poll, "typically while the price is still walking up through the 60s
and 70s", so it quotes an earlier, cheaper, less certain setup than the one the
trading path later takes. The alert and the trade are not the same event.

ONLY ONE BASKET HAS ENOUGH HISTORY TO TEST.

    basket         n  days  all-win   cost  combo ROI  singles    diff        b/e
    BTC+ETH      195     3    60.5%  0.537     +11.7%   +1.2%  +10.5%      1.13x
                                                  [+9.8%, +11.5%]

Everything else spans one or two days. A day-clustered bootstrap over a single
day resamples that same day on every draw, so its interval collapses to a POINT
- rows like [+26.5%, +26.5%] - and its p-value floors at 1/draws. A first pass
reported "26 of 26 baskets survive Holm-Bonferroni", which was an artifact of
exactly this: zero uncertainty where there should be almost none available. The
script now marks anything under three days UNTESTABLE and excludes it from the
Holm family. A collapsed interval is a symptom of no data, never a strong result.

WHAT BTC+ETH SAYS, and what it does not. Taking both legs as a combo turns
+1.2% per dollar into +11.7% per dollar on the same 195 windows. The direction
matches the 68-day replay in #88, which is mild corroboration from an
independent construction. But three days is three clusters, and three clusters
is not a result.

AND THE VENUE STILL DECIDES. Break-even is 1.13x the product of the legs' asks.
The combo orderbook filled a resting bid at 1.10x (#80) - inside, by three
points. The app/RFQ path charges 1.75-2.42x - far outside. So even if this holds
up, it is only executable by resting a bid on the combo's own book, and the
margin for error there is about three percent.

NEAR IS BLEEDING AND SHOULD BE LOOKED AT SEPARATELY: 16 alerts, 50.0% win
against a 0.745 mean ask, a residual of -0.2450. One day and sixteen alerts is
far too little to condemn it, but it is the only instrument whose alerts are
underwater and it is worth watching rather than filing.

WHAT WOULD MAKE THIS TESTABLE: nothing but time. The alert record starts
2026-09-21 for BTC and later for everything else, and the fix is to re-run this
script in two or three weeks, when the shared windows span enough days for the
day-clustered interval to mean something. Nothing needs to be built.

## 90. The intelligence is not learning because the feature contract keeps resetting its evidence

The operator's observation: hundreds of alerts have been sent and the
intelligence layer has promoted nothing. That is correct, and the cause is
structural rather than a shortage of alerts.

FOUR FEATURE CONTRACTS IN THREE DAYS. `learning_data` excludes any live decision
whose `feature_version` is not the current one - deliberately, because a decision
taken under different definitions describes a cell that does not exist under
these. On BTC that discards 6,395 of 8,982 decisions, 71%:

    brti-1   1756 rows   09-23 01:04 .. 09-23 18:38   0.7 days
    brti-2   4566 rows   09-23 18:49 .. 09-25 17:23   1.9 days
    brti-3     73 rows   09-25 17:34 .. 09-25 18:08   34 MINUTES
    brti-4   2587 rows   09-25 18:19 .. 09-26 20:08   1.1 days

Every instance was reset by the brti-4 change on 09-25 18:19, so the WHOLE
system's live evidence is at most 1.1 days old:

    asset   current era   qualified evidence   alerts in that era
    BTC           1.1d                   32                  102
    ETH           1.1d                   46                  101
    SOL           1.1d                   65                  101
    GOLD          1.1d                    8                   10
    SILVER        1.1d                    0                   10
    XRP           0.4d                   19                   33
    NEAR          0.2d                    7                   22

177 qualified signals across seven live instruments. Silver has ZERO and cannot
learn anything at all. Meta-labelling was measured (FINDINGS 38) to need ~10,700
qualified signals; at the current ~16/day on BTC that is 1.8 years, and the next
feature change returns it to zero.

THE ALERTS ARE NOT THE LEARNING UNIT, which is the other half of the gap. An
alert fires once per window at the first poll above the manual floor; evidence
requires a `base_qualified` decision with a graded outcome. Roughly 379 alerts in
the current era yield 177 qualified rows. That ratio is expected and is not the
problem - the 1.1-day horizon is.

AND SEPARATELY, THE LEARNER IS BEHAVING CORRECTLY. Fitting is not starved: it
trains on 6,562 rows because the corpus is backfilled to brti-4. Of 29 arms
fitted, 9 eligible, 11 testable cells, 3 pass the confidence bar and 0 survive
Holm-Bonferroni; 0 of 2 execution cells survive. The arms' own recorded reasons
say why, and they are not the reasons of a broken system:

    "cell does not point against the base decision"          (most arms)
    "veto proposed; train -0.0043 and validate +0.0288
     disagree in sign"                                        (the one that did)

So most cells AGREE with the deployed gates - there is nothing for a confidence
arm to add - and the single dissenting cell fails to replicate out of sample.
That is an honest learner finding the hand-fitted gates already capture what is
available. `baseline_accept` +0.0151 against `baseline_reject` -0.1211 is the
same statement from the other side.

TWO SEPARABLE CONCLUSIONS, and they need different responses:

  * LIVE EVIDENCE CANNOT ACCUMULATE while the feature contract moves. This is
    fixable and the fix is a decision, not code: FREEZE THE CONTRACT. Every
    change costs the entire live evidence base, and brti-3 lived 34 minutes.
  * NOTHING CONTRADICTS THE BASE RULE even on 6,562 corpus rows. Loosening the
    promotion bars to make the learner "do something" would convert this honest
    null into shipped noise, and must not be done.

A COSMETIC DEFECT THAT MAKES THIS HARDER TO SEE: 18 activations are logged with
reason "no regression on the validation slice (+0.0000)" while `new_pnl ==
current_pnl` exactly and `new_changes` is 0. The system is activating identical
fits, so the activation log reads like progress when nothing has changed. Worth
suppressing an activation when the comparison is exactly zero, so the log only
records real movement.

## 91. The LLM is not in the learning loop because it was never wired to it

The operator asked why the local model does not appear in learning, noting the
full lifecycle is collected for exactly that purpose. Checked directly; four
separate reasons, and none of them is a malfunction.

  1. IT IS NOT RUNNING. `settings.brain_url` is `http://127.0.0.1:8080/v1` and
     nothing listens there, nor on 11434 or 1234. The runtime and three models
     are present at `D:\Kalshi\llm\` and simply not started.
  2. ITS ONLY WIRING IS POST-ALERT COMMENTARY, AND THAT IS OFF.
     `brain_commentary_enabled` defaults False, disabled 2026-09-21 because
     "the entry alert now carries the checks, the context, the confidence
     arithmetic and the similar-regime read, so a second message restated all
     of it in prose."
  3. IT HAS NO CONNECTION TO LEARNING AT ALL. `brain` appears nowhere in
     `learning.py`, `learning_runner.py`, `learning_data.py`,
     `intelligence.py`, `intelligence_policy.py` or `adaptive.py`. It is
     imported once, in `main.py`, on the alert path.
  4. IT IS ARCHITECTURALLY FORBIDDEN FROM DECIDING, by its own docstring:
     "Python computes every number; the model only writes prose" and "It never
     gates a trade... the trading path is bit-for-bit unchanged."

So it was never part of learning. The RUNBOOK classifies it as "commentary
only" and "additive", and that is exactly what it is.

THE OPERATOR IS RIGHT THAT THE DATA IS THERE. Per instrument the store holds
`observations` (60 columns, including book depth, order state, exit reason and
realised P&L per poll), `decision_records` (41 columns), `decision_details`
(the full rendered narrative per decision), `intelligence_decisions` (50
columns), `realised_events`, `executions`, `fills` and `recovery_adds`. Nothing
reads any of it for learning.

WHERE A MODEL CAN LEGITIMATELY HELP, and it is a real gap rather than a
courtesy. The statistical learner keys on distance x price x momentum, declared
IN ADVANCE and deliberately so - FINDINGS notes the keying was fixed before
fitting precisely to stop it being chosen on which partition lit up. The
consequence is that the learner can only ever learn WITHIN that keying. It
cannot notice a pattern nobody keyed for, because such a pattern has no cell to
live in. That is a structural blind spot, and it is exactly what reading the
lifecycle record can address.

THE DIVISION THAT PRESERVES EVERY EXISTING GUARANTEE:

    the model PROPOSES a hypothesis, as a machine-checkable filter over the
    lifecycle record - never a number, never a decision

    the EXISTING pipeline disposes: the proposal is compiled to a filter, run
    over the corpus, and tested by nested out-of-sample calibration, the
    day-clustered bootstrap and the multiplicity correction already in place

    nothing is adopted on the model's say-so, and no proposal can reach an
    order without clearing the same execution bar as any other cell

That keeps "Python computes every number" intact - the model contributes a
QUESTION, and the arithmetic answering it is the arithmetic already trusted. It
also makes a wrong hypothesis free: it fails its test and is discarded, which
is the same fate as any cell that does not replicate.

WHAT IT CANNOT FIX. The feature-contract churn of FINDINGS 90 still governs how
much live evidence exists to reason over. A model reading 1.1 days of lifecycle
is reasoning over 1.1 days of lifecycle.

## 92. Two ways a healthy system was made to look broken, and one real trap

Both of these happened while deploying the evidence-bar change, and both are
recorded because the mistake is cheaper to read than to repeat.

A STALE PID FILE IS NOT A DEAD SERVICE. `runtime/service.pid` is written ONCE,
at startup, so a service up for seventeen hours has a seventeen-hour-old pid
file and is perfectly well. Reading that age as staleness - and reading a
two-minute-old log line as "stopped", when two minutes is an ordinary gap
between windows - produced a confident report that BTC had been down for twenty
minutes. It had not. It was polling, deciding and logging throughout, and the
duplicate starts attempted against it were refused by the single-instance lock
exactly as designed. The lock working is not the lock failing.

What proves a service is alive is the LOG STILL MOVING. `full_circle_check.py`
now tests that, and uses the pid file's age only to distinguish a hung service
from a recycled PID when the log HAS stopped - two conditions needing different
fixes.

THE BTC SERVICE CANNOT BE KILLED FROM AN ORDINARY SHELL. It is started by the
`BTC15Signal` scheduled task in a different security context: `taskkill /F /T`,
`Stop-Process -Force` and WMI `Terminate` all return access denied, `Win32_Process`
returns a null CommandLine and a blank owner, and `schtasks /end` reports
SUCCESS while the process survives. Only an elevated shell can stop it. The
other six instances launch from `scripts/run_*.ps1` and kill normally.

AND THE REAL TRAP: `train_intelligence.py --help` OVERWRITES THE LIVE POLICY.
The script took no arguments, so an unrecognised flag was not an error - it just
ran, and replaced a live 29-arm brti-4 artefact with a 7-arm one carrying NO
feature fingerprint, which `compatible()` treats as incompatible and would have
refused on load. Nothing reached an order: the service held the good policy in
memory, all 51 decisions in the following half hour still named
`kalshi-brti-4-1790446662`, and the file was restored from
`runtime/policies/kalshi-brti-4-1790446662.json` before any reload.

It now requires `--write`, and says why:

    refusing to overwrite the live policy without --write. The running service
    loads that file; rewriting it is a deployment, not an inspection.

A script whose only mode is "rewrite the artefact the live service loads" must
not treat an unrecognised argument as consent. That `runtime/policies/` keeps
every generation is what made this a five-minute recovery instead of an
incident.

WHAT ACTUALLY NEEDED THE RESTART, once the false alarm was cleared: BTC has been
running since 00:07:31, which predates the evidence-bar and FDR changes, so it
alone still holds the old constants in memory. The other six restarted at 17:20
and carry the new ones. A policy is written at FIT time, so `min_evidence` moves
from 120 to 100 on each instrument's next learning run rather than at restart -
expected, and only a fault if it survives a settlement batch.

## 93. The feature contract is frozen

Operator decision, 2026-09-27. `brti-4` / `a641ab8e2a05aa44` is pinned as a
literal in `tests/test_feature_contract.py`, and changing the contract now fails
the suite with the cost named.

WHY A PIN AND NOT A COMMENT. There were already seventeen tests on this
contract, and all seventeen keep passing when it changes - they compare the
contract to ITSELF, asserting that a changed lookback changes the hash and that
a mismatch is refused at runtime. Every one of those is about the MECHANISM.
None pinned the VALUE, so four contract changes in three days passed a green
suite. The freeze is two literals:

    FROZEN_FINGERPRINT = "a641ab8e2a05aa44"
    FROZEN_VERSION     = "brti-4"

and the only way past them is to edit them, in the same commit, which puts the
reason in front of whoever does it.

WHAT IT COSTS TO CHANGE, restated in the failure message rather than left in a
docstring: `learning_data` excludes every live decision whose `feature_version`
is not current - correctly, since a decision taken under other definitions
describes a cell that does not exist under these. So a contract change DELETES
THE LIVE EVIDENCE BASE. Measured on BTC: 6,395 of 8,982 decisions, 71%.

THE GUARD WAS MUTATION-TESTED, because a guard nobody has seen fail is a guard
nobody knows works - which is the same defect as the gold alert that counted
five thresholds no input could fail as five passed checks (FINDINGS 74).
`momentum_window_s` 300 -> 301 and `volatility_window_s` 300 -> 420 were each
applied to the real module; the freeze failed on both and named the drift, and
the contract was restored.

ONE DEFECT THE MUTATION TEST FOUND IN THE GUARD ITSELF. The self-check
originally built `FeatureContract(momentum_window_s=301)` from a literal. Under
the 301 mutation that literal became the LIVE value, so the check collided with
the thing it was checking and failed for the wrong reason. It now perturbs
`fc.CONTRACT.momentum_window_s + 1`, derived, which cannot collide.

THE BASELINE, recorded so the freeze's benefit is measurable rather than
asserted - `runtime-combo/evidence_baseline_at_freeze.json`:

    asset   qualified   brti-4 rows   oldest
    BTC            44          3197   09-25 18:19 UTC
    ETH            60          3372   09-25 18:19 UTC
    SOL            78          3417   09-25 18:19 UTC
    XRP            28          1572   09-26 11:48 UTC
    NEAR           12          1299   09-26 14:34 UTC
    GOLD            8           291   09-25 18:19 UTC
    SILVER          0           290   09-25 18:19 UTC

    230 qualified across seven instruments, none older than 1.7 days

That was 177 a few hours earlier, so roughly 18 an hour system-wide. Against the
operator's threshold of 100 per instrument, SOL (78), ETH (60) and BTC (44) are
the ones that cross first, and they only cross because the contract stopped
moving. Metals accumulate only on weekdays.

TO UNFREEZE, which is allowed and will sometimes be right: change the contract,
update both literals in the same commit, and record in FINDINGS what was reset
and why it was worth resetting. The point was never that the contract must never
change. It is that it must not change by accident, and four times in three days
was an accident each time.

## 94. A combo recovery lost to the current single-leg recovery on the real record

Operator proposal, 2026-09-27: keep every recovery rule as deployed (armed by a
loss, 5-market window, once per loss, recovery leg in 0.70-0.79, $2.00 budget)
but make the ORDER a combo of that instrument plus another instrument whose own
rule qualified at the same moment, adding one check - the combo's profit if
right must net the loss being recovered to zero or beat it. The operator's
instruction was to replay it on the signals actually generated and not to lean
on earlier findings, so nothing from #80-#89 is assumed here.

THE REPLAY (scripts/recovery_combo_replay.py). The bot's real trades, one per
window, read the way `Store.settled_bot_markets` reads them, at the decision ask
from `decision_records`. Partners are other instruments' real rule-qualified
decisions from their own stores, taken as of the recovery's decision instant and
never later - the no-lookahead requirement is an assert, not a comment. Sizing
is replayed under today's rules for both policies from base 1, because
`count > 1` in the record mixes a 2-contract base and an older recovery path.
Held to settlement, no fees. Each instrument is replayed only from the first
moment another instrument was recording, so a missing partner means one was not
qualified, not that it was not logged.

    policy                   fired  won  netted the loss   instance P&L
    A  single-leg (deployed)    19   17               0          -0.70
    B  combo                     6    4               4          -1.51

    B - A   -0.82 over 4 days, behind on 4 of 4 days

WHY B LOST, all three measured:

  * THERE IS USUALLY NO PARTNER. In 19 of the in-band armed moments no other
    instrument was rule-qualified at that instant, so B held back and the trade
    went out at base size - while A recovered, and A's recoveries won 17 of 19.
    Three more were held back by the net-zero check itself.
  * THE CHECK BUYS A HARDER BET. To net a ~0.80 loss the combo must be cheap,
    around 0.55-0.64, which means both legs must win. Combos won 4 of 6; each
    loss cost 1.80-1.89 against A's 1.40-1.44.
  * A WAS GOOD HERE. In-band single-leg recoveries won 89% over these four days.

NOT AN ARTIFACT of the two choices most likely to manufacture it. Partner
freshness from 60s to 900s changed nothing - identical results at every
setting, so the absent partners were genuinely unqualified, not stale. Pricing
does not rescue it either: B - A is -0.60 at 0.98x the product (the price the
app actually quoted on this shape tonight), -0.82 at 1.00x (the orderbook) and
-2.35 at 1.10x.

ONE THING A NEVER DOES: net a loss. 0 of 19. Two contracts at 0.70-0.79 win
0.42-0.60 against losses of 0.72-0.90, so the deployed recovery dents a loss and
never clears it in one trade - while still earning +5.36 on its own recovery
trades. B cleared the loss 4 times in 6. That is the real trade-off: B recovers
completely when it fires and wins, but fires rarely and loses bigger.

THE SAMPLE IS SMALL and this is a replay, not a proof: 19 and 6 recoveries over
4 days. What it does establish is that nothing in the real record supports
switching, and the direction was the same on every day and every setting.

AND 87 OF 140 REAL TRADES WERE CASHED OUT EARLY (BTC 39/57, ETH 48/83), which
this holds to settlement for both policies. The comparison is like-for-like, but
neither total is what the account actually earned.

SUPERSEDES #82 ON ONE POINT. #82 recorded dollar-target RFQs as never quoted
(0 of 8). Tonight the operator's `target_cost_dollars` $2.00 RFQs on BTC-down +
NEAR-up were quoted 3 and 4 times each, at 0.718-0.758 against a leg product of
0.7296 - 0.98x. The shape of the ask still decides a lot, though: of about 104
two-leg RFQs in that quarter hour only that one combination was answered, and
BTC-down + ETH-down went 0 for 36. Executability, not price, is the binding risk
for any combo recovery.

## 95. Account-level recovery through a BTC+SOL combo: it almost never forms

The operator narrowed #94 twice: the combo must be BTC+SOL only, and ANY loss -
including ETH's - is recovered by that BTC+SOL combo, with ETH never upsizing.
Replayed with `scripts/recovery_combo_replay.py --mode account` (now the
default) over the span where both BTC and SOL were recording decisions, which is
09-25 15:34 onward.

Two readings had to be chosen and are switchable rather than buried: the 5-market
life is counted in 15-minute WINDOWS, so three instruments settling per window
do not shorten it to ~2 windows; and a window with several losing markets is one
episode whose loss is their SUM (or, as the alternative, only the largest).

    loss to net              0.98x    1.00x    1.10x    combos fired
    sum of window losses     -1.19    -1.19    -1.19    0
    largest single loss      -0.28    -0.32    -1.19    1 (won)

    B - A on the whole account; A is today's per-instrument single-leg recovery,
    which fired 13 times, won 11, and netted a loss 0 times

THE COMBO HAS NOTHING TO FORM FROM. Over the whole record BTC and SOL were
rule-qualified at the same instant in 14 of the 165 windows where BTC qualified
- 8%. In the armed, in-band moments of this replay the other half of the pair
was qualified 1 time in 17. So account-level B mostly just removes today's
recovery, whose single-leg trades won 11 of 13, and loses exactly that upsize.

AND WHEN IT CAN FORM, IT MAY NOT FILL: that evening BTC-down + SOL-down RFQs went
0 for 17 quoted.

The per-instrument BTC+SOL variant came out +0.45, but that rested on ONE combo
(+1.08) and on skipping one losing SOL recovery - two events, not a result. The
account-level rule the operator specified is the one reported here.

The overlap is about 1.3 days, so none of this is conclusive. It is, again, the
direction on every reading tried, and the 8% coincidence rate is a structural
number rather than a noisy one: it is what limits the idea, and it will not be
fixed by more days of the same.

## 96. The operator's final pairing: each loss recovered by a combo with its SOL/BTC partner

#95 rested on a misreading. The operator's rule, stated across three messages:
each instrument recovers its OWN loss; BTC's partner is SOL, ETH's partner is
SOL, SOL's partner is BTC only; BTC+ETH is never a recovery combo. Replayed with
`scripts/recovery_combo_replay.py` (per-instrument, now the default; `ALLOWED`
holds the pairing). Each instrument is judged only from when its partner was
recording.

                   today (A)                  combo (B)
    BTC      4 fired, 4 won    -0.17     1 fired, won, netted   -0.18
    ETH      7 fired, 6 won    +0.64     0 fired                 -0.13
    SOL      2 fired, 1 won    -0.57     0 fired                 -0.11
    total                      -0.10                             -0.42

    B - A   -0.28 at 0.98x product, -0.32 at 1.00x, -1.19 at 1.10x;
    identical at partner freshness 60s, 120s and 900s; behind on 0 of 3 days
    (it was behind on the one day that differed and level on the other two)

The one combo that formed was BTC-down + SOL-down at 05:15 on 09-26: 3 at 0.640
against a 0.85 loss, won +1.08 and netted it. Every other armed, in-band moment -
22 of them - had no partner qualified at that instant, and there the combo rule
simply forgoes today's recovery, whose single-leg trades won 11 of 13.

THE PARTNER RATES, over the whole record, at the same instant:

    BTC qualified in 165 windows - SOL also qualified then in 15   ( 9%)
    ETH qualified in  99 windows - SOL also qualified then in 20   (20%)
    SOL qualified in  77 windows - BTC also qualified then in 15   (19%)

ETH+SOL coincides twice as often as BTC+SOL, yet never landed on an armed,
in-band ETH moment in this span. These rates are structural - they come from the
instruments' own gates - and are what limits the idea; more days will narrow the
error on B - A but will not lift them.

Sample: 3 days, one combo. Not conclusive, but nothing in the record supports
switching and the direction held on every setting.

## 97. The 3-step combo recovery chain, tested forward on real opportunities

Rule (operator 2026-09-27): combo 1 must clear the original loss L0; if it loses,
combo 2 must clear at least 50% of combo 1's loss; then combo 3 clears L0 and the
chain stops. $2 budget every step, never escalating; pairing BTC->SOL, ETH->SOL,
SOL->BTC. The real record holds one recovery combo (it won), so the chain was run
forward by resampling real opportunities (`scripts/recovery_chain_test.py`):
trigger qualified in 0.70-0.79 with its partner qualified at the same instant.

    availability      12 of 170 in-band recovery moments had a partner (7%)
    real combos       6 of 12 won at a mean price 0.558
    real single-leg   129 of 170 won at a mean ask 0.746

    per original loss          mean    fully recovered   5th pct   worst
    combo chain (x3)          -1.14             57.6%     -6.19    -5.83 exact
    today's single-leg        -0.78              0.0%     -3.20    -5.29 exact

    chain beats today's recovery once combos win about 56% or more:
    50% -0.36   56% +0.22   60% +0.22   70% +0.80   80% +1.07 per loss

Independent draws shown; day-clustered resampling of 3 days gives -2.65
[-5.94, -0.59] for the chain and -1.89 [-5.26, +0.18] against single-leg, which
is mostly 12 combos on 3 days rather than information. The operator's point
holds - the chain fully recovers the loss 58% of the time and today's recovery
never does - but on the 12 real combos (50% won) it costs more on average, and
the break-even is the combo's own price. The 7% availability is the other limit.

## 98. Combo recovery with an unqualified partner: the partner's PRICE decides it

Operator, 2026-09-27: the recovery combo needs only ONE qualified side - the
instrument recovering its loss, under today's rules - and the partner (SOL for
BTC and ETH, BTC for SOL) trades whatever side its signal shows, so there is
always a match. `PARTNER_MUST_QUALIFY = False` in recovery_combo_replay.py.

ON ITS OWN IT IS THE WORST VERSION TESTED. On the real sequence:

    partner rule             recovery rule        B - A (3 days)  combos  won
    must qualify             either                   -0.32          1      1
    signal side, any price   re-arm every loss        -6.00         11      5
    signal side, any price   3-step chain             -7.10         12      5

The qualified trigger leg won 9 of 12; the unvetted partner leg won 7 of 12 and
sank four combos whose trigger won. The cause is specific: the NET-ZERO CHECK
SELECTS CHEAP PARTNERS, because a cheap partner is what makes a combo cheap
enough to clear the loss - and a cheap partner is a weak signal near a coin
flip. Every losing partner was priced 0.57-0.66; every partner at 0.78+ won.

THE SPLIT HOLDS ON THE WIDER POOL, not just those 12. All 98 real recovery
moments with a partner on its signal side, by the partner's price:

    partner priced   n   combo won   combo edge
    below 0.70      44      27-38%   -0.10 to -0.14
    0.70-0.85       39      71-91%   +0.17 to +0.34
    above 0.85      15      50-67%   mostly fail the net-zero check

and day by day the 0.70-0.85 band is positive on all three days (+0.43 n=2,
+0.20 n=32, +0.25 n=5), below 0.70 negative on two of three.

WITH THE PARTNER PRICED 0.70-0.85, on the real sequence:

    re-arm every loss   B - A +0.42   4 combos, 3 won
    3-step chain        B - A +1.42   5 combos, 4 won

The chain did what it was designed to: 09-26 11:00 ETH-&SOL- lost 1.81, and
step 2 at 11:30 (target 50% = 0.91) won +1.27.

WHAT THIS IS AND IS NOT. The 0.70-0.85 band was read off this data, so the +1.42
is in-sample: FINDINGS' own rule is that a threshold chosen from its own buckets
proves nothing until it holds on data it did not see. The per-day consistency is
encouraging and is not that. 32 of the 39 band moments are one day, and the
result rests on 5 combos. It needs forward validation before money moves on it,
and quote availability for these shapes is still unmeasured live.

## 99. Two single-leg recovery runs per loss instead of one

Operator, 2026-09-27: keep today's recovery, and once its win rate is confirmed,
allow a SECOND run so a loss is fully recovered before the loop closes. Replayed
on every real bot trade (321, BTC/ETH/SOL): after a loss, today's recovery
fires; if it wins but has not netted the loss, a second 2-contract run fires at
the next in-band trade within 5 markets; then the loop closes. Same $2 per run,
never escalating. No fees, held to settlement.

                     total P&L   loops fully recovered   recovery trades won   worst loop
    one run (today)     +4.92          0 of 38 (0%)            32 of 38 (84%)       -2.86
    two runs            +6.66         15 of 29 (52%)           45 of 53 (85%)       -3.70

    two runs ahead on every instrument: BTC +0.54, ETH +0.93, SOL +0.27

WHY IT HELPS, and why that is conditional: a run adds one extra contract at an
in-band ask, and an extra contract is worth exactly (win rate - ask). Two runs
simply apply that edge more often. Across all 106 in-band trades the bot has
made, it won 81.1% at a mean ask 0.753 - an edge of +0.058, 95% +/-0.074, which
still spans zero.

"HIGH WIN RATE" IS THE WRONG TEST. At a 0.75 ask the recovery needs 75% just to
break even; 80% is only +0.05 a contract. The number to watch is win rate MINUS
ask on in-band trades. Confirming +0.05 at 95% takes about 236 in-band trades,
+0.10 about 59; the bot makes roughly 15-20 a day across instruments, so a
couple of days can rule out a clearly negative edge but cannot confirm a small
positive one.

## 100. The combo recovery is running in SHADOW, and the instances now restart at logon

Operator, 2026-09-27: keep today's single-leg recovery live and run the combo
recovery of #98 in shadow. `scripts/shadow_combo_recovery.py` is its own process:
it reads the live stores READ-ONLY, never places or sizes anything, and writes
only `runtime-combo/shadow_combo.db` (log: `shadow_combo.log`). The trading
services were not touched or restarted.

It STARTS FRESH - the chain began at 2026-09-27 03:48 UTC, stored in the db - so
every decision it records is out of sample for the 0.70-0.85 partner band that
#98 read off the earlier three days. It rebuilds from the stores on every pass,
so a restart or a missed pass changes nothing, and it records a decision only
once both legs have settled, because the chain's next step depends on the grade.

VERIFIED AGAINST THE REPLAY before launch: `--report --since 2026-09-25T15:34`
into a scratch db reproduced #98 exactly - the same five combos, 4 won, combo
P&L +2.85, and whole-account today +0.35 vs combo +1.76, difference +1.42.

    python scripts/shadow_combo_recovery.py --report     # results so far

THE LOGON GAP WAS STILL OPEN. The Startup folder was empty: after a reboot only
BTC (its `BTC15Signal` boot task) would have come back, and ETH, SOL, XRP, NEAR,
gold and silver would have stayed down silently. `install_startup_fallback.ps1`
is now installed for all six - NEAR added, it was missing from the default list -
plus `BTC15Shadow-COMBO.cmd` for the shadow. Logon, not boot: an unattended
reboot that stops at the lock screen still waits for a login. Each entry was
run once by hand; the single-instance lock made the duplicate a no-op.

A small trap on the way: the shadow's startup file was first written with
`printf`, which turned `\b` in `D:\Kalshi\btc15-signal` into a backspace and
produced `D:\Kalshitc15-signal` - a path that would have failed silently at
logon. Rewritten with the file tool and verified.

## 101. The wife's mirror stopped because API orders can only spend their market's shard

The operator reported the mirror account had stopped trading. It had: from about
12:50 UTC on 09-26 every mirror entry was refused - 15 on BTC, 23 on ETH, 2 on
SOL over ~15 hours - logged only as "400 Bad Request".

THE CAUSE. A Kalshi account's cash is split by `exchange_index` (shard), and an
API order can spend only the shard its market lives on; every 15-minute crypto
market is shard 2, which `GET /markets/{ticker}` reports as `exchange_index`. Her
account held $30.37 in shard 0 and $0.09 in shard 2. Proven on BOTH accounts
with resting post-only bids far below the market (an unfillable IOC reserves
nothing and proves nothing): hers accepted $0.05 and refused $0.10 and $0.50
`insufficient_balance`; the operator's refused $31 with $79.61 in total but
$25.95 in shard 2.

THE OPERATOR WAS RIGHT THAT NOTHING IS RESTRICTED - in the app. Her transfer
history shows the app moving exactly an order's cost into shard 2 in the same
second as the order: her manual DOGE fill at 04:13:39 carried an automatic
$1.0092 transfer 0 -> 2, and earlier ones exist from 09-24. The API does no such
thing. An earlier statement here that a $10 transfer had funded her DOGE order
was wrong: that transfer was 44 seconds after it.

THE FIX (execution.py, mirror.py, config.py; tests/test_mirror_funding.py):
  * `KalshiExecutionClient.ensure_funds` does what the app does: before an
    order it reads the market's shard and the balance there, and if short moves
    the SHORTFALL plus a 2c-a-contract fee allowance from shard 0 via
    `POST /portfolio/intra_exchange_instance_transfer`, then polls
    `/portfolio/intra_exchange_instance_transfers/{id}` until `complete` -
    Kalshi documents cross-shard transfers as non-atomic. Never a standing
    float; never raises; a failure is noted and the order still goes out so
    Kalshi's answer is what gets logged. Behind `auto_fund`, OFF by default.
  * Mirrors turn it on (`mirror_N_auto_fund`, default True). The PRIMARY does
    not: what the bot may reach on the operator's own account is a sizing call.
    Its shard 2 holds $25.95 of $79.61 today and will drain the same way.
  * `_post` keeps Kalshi's reason in the error instead of just "400".
  * The mirror log carries a `funding:` note, so no transfer is silent.

THE MIRROR NEVER FOLLOWED THE $2 RECOVERY, found while doing this. It sized every
entry from its own base settings - 1 contract - including the entries the
primary's loss step sized to 2. The loss step now marks a recovery entry
(`trader.entry_is_recovery`, read and cleared once so it cannot leak), and the
mirror sizes it from its own `mirror_1_recovery_budget`, $2.00 by operator
instruction: 2 contracts at 0.70-0.79, still capped by the account's max of 2.

Her shard 2 was topped up by hand with $10 to restart the mirror at once; from
now the code keeps it funded. 16 new tests including one that reproduces the
original failure; 69 across mirror, execution and loss step pass.

## 102. At base 2 the recovery switched itself off; it now follows the base

At 00:00 NY on 2026-09-27 the capital review moved all seven instances from a
base of 1 contract to 2 ($79.61 at $30 a contract). Three pieces had been
written for a base of one, and every one of them failed without an error.

  * THE STEP BECAME A NO-OP. `loss_step_size` bought a flat $2: 2 contracts
    anywhere in 0.70-0.79. 2 is not above a base of 2, so it returned the base
    with an EMPTY reason - no upsize, no log line. Live at 00:49 on SOL
    (KXSOL15M-26SEP270100-00): first trade after the 09-26 09:00 loss, ask 0.78
    in band, 0 markets since. It went out at 2 with "auto: sizing 2 contracts - "
    and nothing after the dash.
  * THE MIRROR LOST ITS RECOVERY WITH IT. The wife's account follows the $2
    recovery only when the primary actually upsizes (`entry_is_recovery`), so
    the same SOL entry bought her 1 contract where yesterday's rule gave 2.
  * THE STEP WAS SPENT BY ORDINARY TRADES. `upsized_since` read "spent" as any
    entry with `count > 1` since the loss. At base 2 that is every entry, so
    the first trade after a loss - at any price - used the step up. The SOL
    entry above did exactly that; the step now reads as taken until a new loss.
  * THE ADD-ON STAND-DOWN WAS BACKWARDS, at base 1 as well. It re-ran the step
    at base 1 and the current ask. Once the step HAD sized a position, the
    re-run found that very entry and answered "already taken", so the add-on
    was cleared to rest behind a stepped position. On a base position whose
    ask drifted into the band, it stood the add-on down for an upsize that
    never happened - BTC 09-26 11:51 printed "standing down - position is sized
    by the loss step" for a 1-contract entry. Its test passed because it never
    wrote the position's own row.

THE OPERATOR'S RULE: "Upsizing recovery must follow as well" - "automatically
double" for the step and "automatically scale double" for the add-on. So both
are now PER BASE CONTRACT, and at base 1 both are exactly what they were:

                      base 1         base 2
    loss step         2 contracts    4 contracts   ($2 per base contract, in band)
    resting add-on    +1             +2
    wife's mirror     2 on a step    2 on a step   (her base is 1, max 2)

THE FIX (main.py, store.py, recovery_add.py, recovery_add_runner.py,
messages.py, config.py):
  * the step buys `contracts_for_budget(loss_step_budget, ask) x base`, still
    capped at `loss_step_max_contracts` (8);
  * "above base" means above the base THAT ENTRY WAS SIZED FROM:
    `Store.base_tier_at(created_at)` returns the day's reviewed tier if the
    review had been written by then, else 1 - exactly what sizing read. A
    review that fails at midnight is retried every poll, so the first entries
    of a day can be sized at 1 and later ones at 2; judging the early ones
    against the later tier would let one loss fire the step twice;
  * `add_on_stands_down` reads the entry row: stand down if and only if it
    went out above its base;
  * the add rests `recovery_add_max_contracts x base`, and `evaluate` weights
    the combined average by the real counts, scores base + add, and charges
    every added contract against the cap;
  * RECOVERY ARMED states the contracts at today's base.

Every piece was verified by execution before the fix (scratch stores, a replay
of the SOL decision on a copy of its database) and each fix is pinned: reverting
the step, the spent-check or the stand-down fails 8, 1 and 3 tests
respectively. tests/test_recovery_follows_base.py is new; the two stand-down
tests in test_loss_step.py were rewritten because they pinned the defect.

AN INDEPENDENT REVIEW OF THAT FIX found its inference was still wrong in ways
the tests did not see (24 findings, 22 confirmed by a second agent reproducing
each; none reachable at today's live settings in the plain case). The root was
one design choice: "was this entry the step" was INFERRED afterwards from
`count` and the tier, and both lie -
  * `record_fill` overwrites `count` with the FILLED count, so a 4-lot step
    that filled 2 at base 2 read as a base entry: the step fired again on the
    same loss, the add-on stacked behind it, the mirror recovered twice;
  * the order-path base is `min(budget x tier, tier)`, not the tier, so with
    /autosize below the ask the step (2 on a base of 1) never read as spent
    and fired on every in-band trade of the episode;
  * an IOC that came back empty still "spent" the step, so the retry went out
    at base.
So the facts are now RECORDED when the proposal is created - `base_count` (what
the base rule produced) and `ordered_count` (what was sent) - and "upsized" is
`ordered_count > base_count` on that row. Rows from before fall back to the
tier. Only orders that traded or might have (not pending/unfilled/rejected/
expired; 'failed' still counts - the order may exist) spend the step. The add
is capped at the contracts actually held. The recovery half of the order path
is now `main.recovery_sizing`, so a test runs the real sizing and the mirror
mark (nothing that calls `primary_signal` has a trader). RECOVERY ARMED leads
with the rule ("2x the base") and states the add-on too.

LEFT AS THE OPERATOR'S CALL, from the same review:
  * A step armed by a loss at base 1 fires at the base it fires at: 2x the
    CURRENT base, so 4 after the move to base 2. The review found SOL carrying
    one such arm from 09-26 09:00; by the deploy it had expired after its 5th
    settled market (checked on copies of all seven databases at 04:1x NY:
    nothing armed anywhere), so no step fires until the next loss.
  * The instance daily loss floors ($5 on SOL/XRP/NEAR/SILVER) did not move.
    A 4-lot step's worst case is about 64% of that floor, and the floor is
    checked on realised P&L before an order, not against the order's own
    worst case.

## 103. The 102 deploy crashed ETH and SOL after every fill (NameError)

The operator asked why the system was restarting on its own. Three restarts on
2026-09-27 were crashes, all the same one, and all caused by FINDINGS 102:

    05:36:40 SOL  KXSOL15M-26SEP270545-45  UP   x2   NameError main.py:2877
    05:38:55 ETH  KXETH15M-26SEP270545-45  UP   x2   NameError main.py:2877
    06:21:08 ETH  KXETH15M-26SEP270630-30  DOWN x2   NameError main.py:2877

Every other restart that day was a deploy (00:22, 04:14, 06:30). BTC, still on
the older build, did not crash.

THE CAUSE. Lifting the recovery half of sizing into `recovery_sizing` left one
later line in `primary_signal` reading `recovery_reason`, which no longer
existed there. That line runs only AFTER AN ORDER FILLS, and nothing in the
suite drives `primary_signal` with a trader - so 1,613 tests and a 12-mutant
check all passed, and each service died on its first real fill. The watchdog
restarted it in about 9 seconds.

WHAT IT COST. The orders were already filled, so the money was real and the
broker has it right: all three WON (+0.19, +0.44, +0.15 on the settlements).
No window got a second entry. The wife's mirror booked her own orders
normally. But the crash hit between the order and its bookkeeping, so each
proposal is stuck at 'executing' with no fill recorded: the service did not
manage them (no 90c cash-out, no fill message), the one-position guard does
not count 'executing' - only the 120s spacing and the entry deadline stopped a
second buy in the 06:30 window - and `settled_bot_markets` (fill_price IS NOT
NULL) cannot see them, so they do not count toward a loss step's wait.

THE FIX (06:30, source 26e0487c327a on all six non-BTC instances):
`recovery_sizing` returns `recovery_reason`; `ruff --select F821,F822,F823`
over src/ and scripts/ is now a test (tests/test_no_undefined_names.py) and
finds exactly this line on the broken build.

NOT DONE: booking the three fills from the broker into the local record
(`record_fill` + `finish_proposal`, the service's own calls) - the tool's
permission check refused a script that opens the live databases for writing;
left for the operator.

## 104. One recovery per losing episode: a losing recovery trade arms nothing

Operator, 2026-09-27, after SOL lost two 4-contract recoveries in a row (16:00
-2.98, then 16:15 -3.32, each armed by the loss before it): "Remove the back to
back it should only happen once."

THE RULE. The loss step arms on the most recent losing bot market. If that
market was itself sized by the step - its entry went out above the base it was
sized from (`Store.window_was_upsized`, the same recorded base-vs-ordered test
as FINDINGS 102) - nothing is armed and the trade goes out at base: "last loss
was the recovery trade itself - no second recovery". The next ORDINARY loss
arms normally. A winning recovery changes nothing.

WHAT IT WOULD HAVE DONE ON THE RECORD (all 35 steps, broker P&L, replayed):
three steps were armed by a losing step -

    09-24 17:45 ETH  6 contracts  won  +1.08  -> at base +0.18   (-0.90)
    09-26 07:45 ETH  2 contracts  won  +0.49  -> at base +0.24   (-0.25)
    09-27 16:15 SOL  4 contracts  lost -3.32  -> at base -1.66   (+1.66)

net +0.51. Three cases decide nothing; the point of the rule is the bound - no
episode can now put two upsized losses back to back.

For context, measured the same day on the broker record: all recovery sizing
together (35 steps, 7 filled add-ons) added +2.22 to the operator's account
(+0.44 all time, -1.78 without it); today's 2x-base steps cost -3.88 of that.
The wife's account followed the recovery only from today: -1.92.

tests/test_recovery_follows_base.py pins it (three tests fail with the rule
removed). test_loss_step.py's fixture wrote every ordinary arming loss at 2
contracts - at base 1 exactly what the step buys - so it now writes them at
base size, 1, as live ones are; its assertions are unchanged.

## 105. The recovery is a combo at base size; nothing upsizes any more

Operator, 2026-09-27: "replace the single recover into a Combo with same base
size, no more up scaling ... everything is kept just as design"; "that's my
decision, ship it live and verify"; "update all messaging systems and update all
tracking to reflect combo"; and, after the first cut: "same direction do not get
rejected - combo accept any direction, either same or opposite".

THE RULE (combo_recovery.py, main.place_combo_recovery):
  * WHEN - unchanged: the loss step decides it (armed by a losing bot market,
    this entry's ask 0.70-0.79, 5 markets, once per episode, never back to
    back). It no longer changes the size: it says a recovery is due.
  * WHAT - this entry plus a PARTNER as one combo: SOL for BTC and ETH, BTC for
    SOL; the partner on whatever side its own signal shows, priced 0.70-0.85,
    read no later than the trigger and no older than 120s, then RE-READ from
    its market before it may set a price.
  * SIZE - the base count (2 at base 2). The wife's mirror copies a confirmed
    combo at her own base (1), with the same price check.
  * PRICE - the cheapest quote at or below the CHEAPER LEG's ask, same or
    opposite direction. A combo pays only if both legs win, so it is never
    worth more than its cheaper leg. The first cut capped at the legs' PRODUCT
    (the shadow's assumption) and live quotes showed that refuses nearly every
    same-direction pair: Kalshi prices correlation in.
        18:37  BTC-DOWN 0.975 + SOL-DOWN 0.986  8 quotes, cheapest 0.983
        18:47  BTC-DOWN 0.64  + SOL-DOWN 0.54   21 quotes, cheapest 0.428
               (product 0.3456 = 1.24x; under the cheaper leg 0.54: BUYS)
        18:52  the app, BTC-UP 76 + SOL-UP 88: $10 pays $13.91 = 0.719
               (product 0.669 = 1.075x; under 0.76: BUYS)
        19:04  BTC-UP 0.63  + SOL-UP 0.84   21 quotes, cheapest 0.688
               (ABOVE the cheaper leg 0.63 - dominated by BTC-UP alone: REFUSED)
    Two-leg RFQs WERE quoted every time (8-21 quotes in 25s) - the "2 legs
    0/137" of FINDINGS 82 did not hold today.
  * THE DROPPED CHECK - "the combo's win must cover the whole loss" can never
    pass at base size (2 at ~0.56 wins ~0.88 against a 2-contract loss of
    ~1.60), so keeping it would mean the combo never fires. Base size was the
    operator's explicit constraint.
  * OUTCOMES - bought: the window's trade, held to settlement, mirrored, told.
    Nothing bought after a quote round: the entry is decided again next poll,
    single-leg at base size (never on a half-minute-old decision crossing to
    the 0.95 ceiling). Unknown (a lost or 5xx accept answer, a quote still
    accepted/confirmed at the deadline, an executed quote with no readable
    fill): HELD, nothing else sent, settled from the broker's fills within
    minutes (`Store.reconcile_combos`). Any combo row but 'unfilled' blocks a
    single leg in its window.
  * THE ADD-ON is off (RECOVERY_ADD_ENABLED=false, shadow): it added contracts.

TRACKING. A combo is a `combo_recovery` proposal on the trigger's window
(legs, price check and outcome in its note); it spends the step; the local
loss rebuild leaves it to the exchange figure (the trigger's `won` is not the
combo's); lifetime record, open positions and /ledger count this system's
combos as the instrument's own (the operator's own combos stay foreign); its
ledger row is dated by the trigger window. MESSAGES: RECOVERY COMBO BOUGHT /
OUTCOME UNKNOWN when placed, RECOVERY COMBO WON/LOST once Kalshi books it (the
window's usual recap steps aside), RECOVERY ARMED describes the combo, the
standing recovery line no longer says "extra sizing allowed".

REVIEWED BEFORE SHIPPING: an independent review found 21 issues, 18 confirmed;
the money ones fixed before deploy - an accept still pending at the deadline
had been booked as nothing bought with a single leg sent on top (HIGH), a 5xx
accept treated as nothing, the stale fallback order, /ledger pricing a combo
by its trigger leg, unknown combos held forever, fills summed across both
instances' positions on the same combo market. tests/test_combo_recovery.py.

COMBO FUNDING (same evening). Combo markets settle in exchange shard 1, which
nothing funded: the operator's account held $0.28 there against $103 in shard 0
and $29 in shard 2, so every combo accept on the main account would have been
refused (the app moves the cost itself; the API does not). Operator: "auto-fund
combos". Before each recovery combo's RFQ the main account now moves the
shortfall for the most it may pay - base count x (cheaper leg + 2c fee
allowance) - from shard 0 into the combo's shard (`combo_auto_fund`, default
on; `ensure_funds(force=True)`), so a good quote is accepted without waiting on
a transfer. The 15-minute orders stay unfunded, as before. The wife's account
funds its combo the same way through its existing auto-fund.

## 106. The base size scales with capital, uncapped

Operator, 2026-09-27 21:xx: "Auto scale and contract should not be cap, it
should scale as capital growth." The account stood at $132.95 after a deposit;
the daily review gives one base contract per $30 of reconciled capital, but
`max_base_contracts` = 2 held it at 2 (the capital supports 4).

  * `max_base_contracts` = 0 now means NO ceiling (`capital.tier_for`). The
    tier is still reviewed once a day, at midnight New York, from reconciled
    capital (cash plus committed cost, never unrealised gains): at $132.95 the
    next review gives base 4.
  * The loss step's own count was still capped at 8 (`loss_step_max_contracts`).
    Since the recovery became a combo at base size that count only answers "is
    a recovery due" (count above base) - so at a base of 8 or more the capped
    count would have equalled the base and ended every recovery silently. It
    is uncapped; no order is ever sized from it.
  * Everything sized from the base follows: normal entries and the recovery
    combo. The wife's mirror is configured separately (fixed 1 contract, cap 2)
    and does not scale.

WHAT DID NOT SCALE, left as the operator's call: the daily loss floors ($5 on
SOL/XRP/NEAR/SILVER, $10 ETH, $20 BTC) are fixed dollars. At base 4 one loss is
about -$3.20, so SOL's floor stops it after two. And instruments lose together
(same-window co-loss 67% against a 16% base rate, measured the same evening on
394 trades): a bad window on three instruments at base 4 is about -$10.

LOSS FLOORS SCALE TOO (same evening). Operator: "loss limit must scale", with
today's limits counted as right for today's base of 2. The day's floor is now
the configured limit x today's base / 2 (`main.scaled_loss_limit`,
`loss_limit_reference_base` = 2): unchanged today; at base 4 SOL/XRP/NEAR/
SILVER $10, GOLD $14, ETH $20, BTC $40 - the same number of losses per day as
today, whatever the size. A Telegram override scales the same way. Before the
day's capital review the base reads 1, so the floor is briefly tighter, never
looser. tests/test_loss_limit_scaling.py.

## 107. The learning loop adopts what it learns; the local model runs in every cycle

Operator, 2026-09-28: "verify system is learning from loses", then "do so the
system learn and adapts". Verified on the live record that morning:

  * BTC activated fresh fits, but none had changed a live decision since 09-26.
  * ETH (since 09-27 02:17) and SOL (since 09-26) REFUSED EVERY FRESH FIT. The
    running policy was compared with the fresh fit on the VALIDATION slice -
    the slice its own promoted rule had been selected on - so it won by
    construction. The loop was learning and throwing the learning away.
  * The one live rule working was SOL's veto. It blocked 34 entries, which
    went 19 won / 15 lost. Taken, they would have lost $2.36 per contract in
    total (fee-free), so the veto saved that.
  * The local model (qwen2.5-1.5b, 127.0.0.1:8080) had run exactly once, by
    hand, for BTC.

THE CHANGE.

  1. FRESH FITS ARE JUDGED ON THE HOLDOUT, the newest third of the
     chronological split. Neither policy was selected on it.
  2. EACH RUNNING RULE THE FRESH FIT DID NOT RE-PROMOTE GETS ONE VERDICT
     (`learning.carry_forward`):
       - running artefact cannot act (changed feature contract, retired
         features, superseded method): NOTHING is carried;
       - live record condemns it (>= 20 changed orders, net cost): let go, and
         left out of BOTH sides of the comparison (`learning.without`) so
         dropping it cannot re-freeze adoption;
       - live record supports it (changed orders, net gain): carried;
       - no live verdict: judged on the holdout, fresh policy with the rule
         against without it, and carried only if it adds value there.
     The activation reason lists what was kept, let go and dropped.
  3. THE LIVE RECORD CAN DECIDE. Every acting rule stays on the candidate
     watch list (`candidate_payload(policy=...)`). The forward record keeps the
     poll where a candidate first DISAGREED with the rule, not the first poll.
     Before this, SOL's forward record had counted 8 of its veto's 35 live
     windows. Seven were lost because refits had dropped the cell from the
     watch list; the rest were first polls taken before the rule accepted.

THE REVIEW CAUGHT TWO THINGS THE FIRST VERSION GOT WRONG (adversarial review,
2026-09-28, 7 of 8 findings confirmed by reproduction):

  * The first version carried rules out of ANY running policy, including one
    that cannot act. At a feature-contract bump the service withdraws the
    running rules on load and a bootstrap fit follows. Carrying from that
    artefact would have silently resurrected a rule fitted under definitions
    this build does not compute. That exact sequence happened at brti-2 -> 3
    and brti-3 -> 4, both times to ETH's veto.
  * It kept a rule forever when its live record was empty. ETH's veto has had
    0 live changes since brti-4 (every such setup fails the distance gate),
    so it could never reach the 20 needed to let it go. On the holdout it
    blocked 181 decisions and cost $1.89 per contract.

A second check of the fixes found four more, all fixed:
  * Rules were judged one after another against a policy whose enable flags
    were set only at the end. A second rule was credited with the first
    one's effect, and a -2.75 veto read +6.50. Flags are now set at each
    carry.
  * A REFUSED fit wrote a watch list without the running rules, which stay
    live. Rules acting in either policy are watched now.
  * Quiet slots were counted from each instance's own finish, so BTC and XRP
    would have hit the model in the same second every 6 hours. Slots are now
    owned by the clock: window index mod 8.
  * A carried rule overwrote the fresh fit's confidence for its cell. It now
    brings only its execution fields.

REPLAY on fresh copies of the live stores, the real `_train`, with each
instrument's strategy file (09-28 ~08:00):

  * ETH: veto DROPPED (no live verdict; -$2.62/contract over 180 blocked
    decisions on the holdout). ACTIVATES.
  * SOL: veto CARRIED (live record +0.95 over 11 changed orders). ACTIVATES;
    its evidence bar becomes 100 (from 120).
  * BTC: nothing acting. ACTIVATES.

THE LOCAL MODEL IS IN THE LOOP (`hypotheses.py`; scripts/lifecycle_hypotheses.py
is now a wrapper). After every completed learning run, the instance's recorded
lifecycles - losses included - go to the local model. It proposes conditions
that separate winners from losers, and each proposal is tested. Nothing the
model says changes an order: a survivor is a candidate for keying that must
still clear the promotion bar.

  * THE TEST. The day-clustered bootstrap reported p = 1/3000 whenever every
    day agreed in sign, which chance does 25% of the time over 3 days. BH then
    passed random filters in 27-98% of runs (null simulation on each store's
    real rows). Now an exact day-level sign-flip test is used: with D days no
    p below 2^(1-D). On today's 3-8 days of history nothing can survive
    before ~7 days, and that is the honest answer.
  * A reply cut off at the token limit used to parse to NOTHING and be
    recorded as a healthy model that proposed 0 (SOL, every observed run).
    Complete proposals are now recovered and the cut is reported. Up to 8
    proposals, 1200 tokens.
  * BH is keyed by position, not by the model's names, which repeat. A
    losing slice is worded "trails the rest by X - one to avoid".
  * TIMING. Each instrument owns the windows whose index mod 8 is its
    position (BTC, ETH, SOL, XRP, NEAR, GOLD, SILVER; `SLOT_ORDER`). The call
    starts 10s after the open and times out at 200s, so it ends before
    entries open at +240s. The wait is at most 2 hours. It runs on a daemon
    thread, so a crashing service is not held (with service.lock) until the
    call returns.
  * OUTPUT. `<policy dir>/hypotheses.json` and `hypotheses_history.jsonl`, a
    log line "learning: local model [ASSET] ...", and a Telegram message only
    when something survives.
  * RESIDUAL BIAS, stated. The model picks thresholds after seeing a summary
    of the same rows it is tested on, and the message says to treat
    survivors as leads.

tests/test_learning_adapts.py.

## 108. Only BTC and GOLD trade live; ETH and SOL go back to shadow

Operator, 2026-09-28 09:0x, final: "only gold and BTC are allowed to trade
live". ETH and SOL return to shadow (they record and alert, and an order needs
Telegram approval). XRP, NEAR and SILVER stay in shadow. GOLD automation is
enabled by this explicit authorization.

THE EVIDENCE BESIDE IT (fee-free, per standing instruction):

  * BROKER RECORD, automatic trades (settlements and fills derived
    independently, agreeing to the cent): BTC +$10.62 over 264 (85% won,
    +2.3c a contract); ETH -$12.31 over 141 (combos included, -4.4c); SOL
    -$4.78 over 40 (-5.4c). BTC alone +$10.62, BTC+SOL +$5.84, all three
    -$6.47. ETH was +$2.54 before 09-28 and lost $14.85 that day at base 4.
  * THE SAME SIGNAL ANALYTIC ON EVERY INSTRUMENT (1 contract, recorded price,
    settled outcome). Would-trade decisions (rule-qualified, not vetoed, band
    held 60s; it reproduces BTC's real result, +2.5c): BTC +2.5c (188), GOLD
    +3.6c (49), NEAR -1.2c, ETH -1.4c, XRP -1.4c, SOL -2.3c, SILVER -13.0c.
    Every alert as sent: GOLD +5.9c (131), BTC +3.5c (661), SILVER +1.3c,
    SOL +0.9c, XRP -0.8c, ETH -1.0c, NEAR -5.1c.
  * PAIRED WITH BTC on shared days, alerts: BTC+GOLD +$18.75 vs BTC alone
    +$11.04, and GOLD lost in 10 of BTC's 33 losing windows. ETH lost in 54 of
    BTC's 89: it deepened BTC's bad windows instead of offsetting them. On the
    3 days all four had alerts: BTC +$7.78, BTC+GOLD +$16.29, BTC+GOLD+SILVER+
    SOL +$19.65 with a worst day twice BTC+GOLD's. On would-trade decisions
    (2 days): BTC +$3.40, BTC+GOLD +$3.68, the four together -$2.74.
  * NOT PROOF. Every 95% interval spans zero. GOLD has 4 days and closes
    about 48h every weekend.

HOW IT WAS APPLIED. The switch is the STORED `auto_trade_enabled` row, which
wins over the launcher's default (`main.auto_is_on`). It was set with
`scripts/auto_switch.py`: ON for gold15.db, OFF for eth15.db and sol15.db,
and an explicit OFF for xrp15/near15/silver15, which had relied on the
default. run_sol.ps1 no longer defaults auto on, run_gold.ps1 does, and all
three launchers carry the decision. ETH, SOL and GOLD were restarted at a
window open after a broker flat check.

WHAT FOLLOWS FROM EXISTING CONFIGURATION:
  * The mirror copies GOLD (MIRROR_INSTANCES lists gold) at 1 contract, cap 2.
  * GOLD has no combo partner, so its recovery is a single entry at base size.
  * GOLD's floor scales with the base: $14 a day at base 4.
  * OPEN: BTC's recovery combo takes its second leg from SOL
    (combo_recovery.PARTNERS), so a BTC recovery still carries SOL exposure.
    Put to the operator.

CORRECTION, SAME MORNING (independent recheck, after the decision was
applied). The GOLD evidence above does not hold under the gates the live code
applies. The auto path's 60s settle timer (main.py ~2888) measures
strategy.json's 0.70-0.93 band FOR EVERY INSTANCE, while the recorded
`band_hold_s` uses the instrument's own band (GOLD 0.60-0.80). The live
decline log confirms this on SOL: 58 of its would-trade windows were refused
"price has only held the band", and SOL never ordered below 0.70.

  * GOLD's +3.6c came entirely from asks below 0.70 (22 signals, +$2.52).
    At 0.70 and above it was -$0.77, and that is the only range the deployed
    auto path can trade.
  * Under the live gates: GOLD -$0.35 over 30 (-1.2c, CI -15c..+10c);
    BTC+GOLD +$3.52 vs BTC alone +$3.87 on shared days, and +$3.77 vs +$4.12
    over the whole record. No shadow instrument improves on BTC alone.
  * The +3.6c also rested on one day (09-25; without it -0.6c) and on about
    33 trading hours across two rule versions (brti-2 band 0.65-0.75, brti-4
    0.60-0.80).
  * "GOLD lost in 0 of BTC's losing windows" was 0 of 2. GOLD was closed for
    8 of BTC's 11 losing windows on its days.
  * The daily loss floor is ACCOUNT-WIDE: auto_state takes the whole
    account's settlements. ETH was stopped on 09-24 at its own -$1.17, and
    SOL on 09-26 at its own -$1.55. BTC's losses can stop GOLD and vice versa.
  * Grading from Kalshi's official settlement values changed no number.

Put to the operator at once. GOLD stays ON unless the operator says otherwise:
the decision is theirs, and the evidence is recorded beside it.

THE OPERATOR'S ANSWER TO THE CORRECTION (same morning):
  * GOLD: "Use the numbers that work for Gold ... the one that shows Gold did
    not lose together with BTC". Gold stays ON, and it trades on its OWN band.
    The auto path's settle timer now measures the instrument's own band
    (`settle_rule = kalshi_rule`), so gold can enter 0.60-0.80 as its rule
    qualifies. BTC and ETH carry 0.70-0.93 in both files and are unchanged.
    The evidence for gold is the would-trade analytic (+3.6c over 49), with
    the caveats above: it rests mostly on 09-25, and GOLD was closed for 8
    of BTC's 11 losing windows.
  * BTC's recovery: "BTC AND GOLD ONLY I SAID". BTC no longer has a combo
    partner (`combo_recovery.PARTNERS`). Its recovery is a single entry at
    base size, and nothing upsizes. ETH/SOL keep their partners on paper but
    are in shadow.
tests/test_live_instruments.py pins the settle band, the partner map and the
launchers.

REVIEW OF THE BTC+GOLD CHANGE, before deploy (fixed):
  * THE ENTRY CEILING WAS BTC'S. Every IOC is sent AT `max_entry_price`
    (0.95) and the mirror copies it. For gold (band 0.60-0.80, negative
    above 0.85 on every sample; its ask jumps >=10c between polls 3.8% of the
    time, against 1.05% for BTC) a 0.66 setup could fill at 0.95. The auto
    path's ceiling is now min(0.95, the rule's own max_ask + entry_slippage):
    gold 0.85, BTC and ETH still 0.95. On BTC, 62 of 265 fills already came
    in >=3c above the decision ask, and 19 above the `limit_submitted` the
    executions archive recorded, so that column misstates the real limit.
  * The status line said "recovery by combo at base size" on every BTC and
    gold message. It now says "every entry at base size, no combo".
  * With no partner, "recovery due" was never spent, so it labelled every
    in-band entry for five markets and re-armed after a loss. Instruments
    with no partner now carry no recovery label. The count was always base.

OPEN, put to the operator (pre-existing, not changed):
  * The daily floor's exchange half is ACCOUNT-WIDE. One -$13 BTC market stops
    gold at -$14 while gold is flat.
  * `execution.market_open_ms` reads the ticker's ET close as UTC. Markets
    closing 00:15-03:45 ET (00:15-04:45 in EST) fall outside their own New
    York day. That turns off the account-wide half of the floor overnight
    and drops them from Telegram's "today" (09-28: -$20.15 across 17 markets
    missing, today -0.31 shown vs -21.36 real). An instance's OWN trades still
    count through the local rebuild.
  * The local rebuild prices a trade by the window's FIRST recorded side. A
    trade placed after a side flip books the opposite result (e.g. SOL
    271615: +0.68 booked, -3.32 settled). This affects BTC 4/265, SOL 2/39 and
    ETH 1/137 fills.

NO RECOVERY AT ALL (operator, same morning): "does the system even need
recovery ... we need no recovery at all ... The current Gold and BTC can
actually run without recovery base on the report we have already seen."
`config.recovery_enabled` = False, the master switch:
  * no loss step: every entry is base size, and no entry carries a recovery
    label;
  * no RECOVERY ARMED / SIZE ENDED message (the deficit is still folded each
    poll as bookkeeping; nothing reads it to size an order);
  * no recovery line on any trading or money message.
The resting add-on and the upfront upsize were already off. The test suite opts
back in (tests/conftest.py) so the mechanics stay tested for re-enabling;
tests/test_live_instruments.py pins the OFF default.

NEXT, being measured: the operator's late high-probability combo idea: BTC +
GOLD legs at 90-98% about 2 minutes before close, $1 a ticket, every window,
about 10% a win.

## 109. The late high-probability BTC+GOLD combo: fairly priced, so the quote decides

Operator, 2026-09-28: "the combo system can be another strategy at very high
probability like 95, 90, even 98% at close to 2 minutes expiry. Every single 15
minutes. Making 10% and testing that strategy with just $1." Two manual $1
BTC+GOLD combos that day both won (+$0.111 each fee-free, both filled at
0.899). Measured read-only before building anything:

  * LEGS AT 2 MINUTES ARE FAIRLY PRICED. For 90-98% favourites at 120s, BTC is
    -1.0c a contract (2,617 windows, 76 days, CI -1.8..-0.3) and GOLD -0.2c
    (51 days). The favourite premium of FINDINGS 1 is real 6-7 MINUTES out
    (BTC +1.6c, GOLD +1.8 to +2.4c, clear of zero) and gone by 3 minutes.
    The "sure thing" loses about 1 time in 20: 10-14% at 0.90-0.92, about 2%
    at 0.97-0.99.
  * THE COMBO. Both legs in 0.90-0.98 at 120s: 284 tickets over 43 GOLD days.
    Both won 88.03%, against 90.62% implied by price. The legs are
    independent (phi -0.06; 0 double losses). At $1 a ticket: -$7.95
    (-2.80%) at the leg product; -$4.87 at 1c under; -$19.79 at the 4.8c
    markup seen on a $10 quote. Break-even is 2.53c UNDER the product
    fee-free (3.31c with the fee). At the product the loss is not proven
    (CI spans zero); at the markup it is.
  * NOT EVERY 15 MINUTES: about 7 qualifying windows per GOLD weekday, none
    on Saturdays. "10% at 95-98%" is arithmetically impossible (a win at p
    returns (1-p)/p: 5.3% at 0.95, 2.0% at 0.98). 10% needs about 0.95 x
    0.95, and that bucket won 87.34% against 90.9% needed.
  * QUOTES. Makers answer in 15-157 ms right up to the close. At $1 the best
    quote is a median 1.007x the product (0.879-1.040; 11 of 36 at or under).
    Accept-to-fill takes 1.1-4.5s. FINDINGS 82's "makers rarely quote" was an
    artifact: GET /communications/quotes keeps only ACCEPTED quotes and quotes
    on still-open RFQs.
  * A $1 test would take about 833 tickets (about 120 GOLD weekdays) to tell
    88.0% from 90.9%. A few weeks would measure the PRICE obtained against
    the product, which is what decides it.
Scratch: late_combo/ (session scratchpad). Put to the operator.

DECISION (operator, same day): "Drop it". Not built. The measurement stays as the record, and the 6-7-minute favourite premium is the open lead if combos are ever revisited.

## 110. The daily loss limit: right day, right side, own instrument; alerts for BTC and GOLD only

Operator, 2026-09-28: "address all 4", then "everything goes out now" and gold
"must be live right now". Shipped together at a window open.

  * THE CLOCK. `market_open_ms` read the ticker's New York CLOSE as UTC, so
    every stored `window_ms` sat 3h45m early. Markets closing 00:15-03:45 ET
    fell out of their own day: on 09-28, 17 markets and -$20.14 were missing
    (-2.01 counted, -22.15 real). The fix re-derives 14,684 stored values
    across the 7 stores, on every Store() open, idempotently. The ticker read
    in New York time agrees with Kalshi's own times 4,305 of 4,305. The "one-
    time" day-boundary carry had re-run at every midnight and would have
    double-counted the previous evening once the clock was right, so it is
    retired. Settlement lands 5-8s after close, not "hours late"; the
    comments now say so.
  * THE SIDE. The local rebuild graded a trade by the signal's first side
    (`TRADE_WON_SQL` now: broker result on the trade's own ticker, else the
    signal re-expressed for the held side, NULL for combos). 3 held trades
    were ever mis-graded ($8 gross, $0 net); SOL 271615 lost $3.32 and was
    booked +$0.68.
  * SEPARATE FLOORS. The floor's exchange half read the WHOLE account, so one
    instrument's losses stopped another. With the clock fixed, the morning's
    ETH/SOL/BTC losses would have stopped gold at $14 while gold was flat.
    Each store now counts its own series and the combos it bought, which is
    what the launchers always said ("each carries its own daily loss floor").
  * TELEGRAM. Only instruments on `telegram_alert_instruments` ("BTC,GOLD"),
    or any instance that is auto-trading, send alerts. A shadow keeps quiet
    except for money that actually moved; its session reaches Telegram in one
    combined SHADOW SUMMARY per session close, sent by BTC: alerts as sent and
    the would-trade decisions, graded, at one contract, fee-free.
  * Also on 09-28: the 3 rows the 09-27 NameError left at 'executing' (ETH x2,
    SOL x1, all won) were booked from the broker's fills, without Store()'s
    migrations. The Telegram token and Kalshi key ID in the committed
    .env.bak files are on a PUBLIC GitHub repo; the private key never was.
    Rotation is the operator's.
tests/test_loss_limit_clock.py, tests/test_telegram_shadow.py.

GOLD'S BAND = BTC'S (operator, same afternoon, 13:17 ET): "set gold to 70-93
same as btc". strategy_kalshi_gold.json min_ask 0.70, max_ask 0.93. It is
re-read every poll, so no restart was needed. The order ceiling follows
(min(0.95, 0.93 + 0.05) = 0.95). AGAINST the measurement, recorded in the
file's _band_comment: on gold's live alerts to date, 60-70c made +10.3c a
contract (55 signals, 76% won), 70-80c 0.0c (54, 74%), 80-93c +5.8c (30,
90%); the 49-day corpus fit put the edge at 0.60-0.80 and found the residual
negative above 0.85 on every sample. The tests now allow a band equal to
BTC's only as a recorded operator decision.

## 111. A second strategy in parallel: ALL-SIGNAL $1 on BTC and GOLD

Operator, 2026-09-28: "this one should be running alongside with the main
strategy already running... trade at a pace one dollar... execute all their
generated signal every 15 minutes. And the main strategy running right now
should keep running as it is. So basically, you will be running two
strategies in parallel."

WHAT IT DOES. On every signal of a listed instrument (`allsignal_instruments`
= "BTC,GOLD": the alert, the first actionable poll of each window) it buys
that side for `allsignal_stake` = $1 - whole contracts, one at 50c and up,
more below - as an IOC at the signal's price plus the usual slippage. No
strategy gate, no band, no intelligence, no daily floor of its own.

HOW IT STAYS OUT OF THE MAIN STRATEGY'S WAY.
  * Its own book, `allsignal_trades`. None of the main strategy's 40-odd
    trade_proposals queries, guards or reports sees a $1 trade, so the
    one-position guard, trade counts, /ledger and recaps are unchanged.
  * It is placed in the background on the alert, so the main poll never
    waits on it.
  * It uses the primary client and is NEVER mirrored to the wife's account.
  * The exchange books one position per market for both strategies, so the
    main strategy's daily floor takes the all-signal strategy's own graded
    money back out (`auto_state`). The $1 test cannot stop or excuse it.
  * The main strategy's exits sell only its own count (reduce_only), leaving
    the $1 contracts. When the two hold opposite sides, Kalshi nets the pair
    at $1, which is the same money as both settling.
  * It stops with /auto off (the kill switch stops everything), and on its own
    with scripts/allsignal_switch.py --db <store> --off.
Reported once a session in the SESSION SUMMARY (real money, separated from
the shadow lines), not trade by trade. The evidence it is measured against:
FINDINGS 110's every-signal replay, +3.8c a contract over 483 signals (BTC
+3.1c, GOLD +5.3c), with a longest losing run of 6 in one broad-market hour.
tests/test_allsignal.py.

REVIEW BEFORE DEPLOY (adversarial, 2026-09-28; fixed):
  * The midnight capital review undercounted. It runs seconds after 00:00,
    while the 23:45 $1 position is still open; its cost was out of the cash
    and not added back, so the main strategy's tier could drop for the day
    ($120.50 read as $119.75, base 4 -> 3). Open all-signal positions now
    count in `open_position_cost`.
  * Interrupted orders are now reconciled. A restart, a cancellation or a
    lost response left rows 'claimed'/'failed' for good, and their real
    result landed in the main floor. `allsignal_reconcile` now settles them
    from the broker's synced fills: the buy on that ticker within a minute of
    the claim that is not a main-strategy order. A fallback price is refined
    the same way.
  * Grading now runs straight after every settlement sync, before the broker
    reads that can fail (a 429 streak was seen live).
  * The fill lookup retries four times, like the main path.
  * Each session summary counts windows by when they SETTLED, so the one
    closing at the boundary is no longer reported "open" and then never.
  * The main strategy's settlement recap takes the $1 contract back out of
    the market's broker P&L.
KNOWN AND ACCEPTED: in about 1.5% of the main strategy's windows it buys the
side opposite the alert, and Kalshi nets the two positions. Money totals stay
exact (settlement = all-signal graded + main as-if-held; the floor
subtraction is right), but the split between the two books is notional there,
and a main cash-out may sell one fewer contract than it records. Changing it
would change the main strategy, which the operator ruled out.

MAIN STRATEGY TO SHADOW; $1 ON BOTH ACCOUNTS (operator, 15:3x ET): "pause the
main strategy and let it run in shadow while we let the new strategy run on
both my wife and mine with the $1 trading all signals... That way we can
evaluate better."
  * The main strategy has its OWN switch now (`main_strategy_on`, stored row
    main_enabled; scripts/strategy_switch.py). OFF on btc15 and gold15: it
    records every decision and places no new entry, and positions it holds are
    still managed to the close. /auto stays the kill switch for BOTH.
  * The all-signal order goes through the mirroring client (`allsignal_mirror`
    = True). A FILLED $1 order is copied to the wife's account, sized there by
    `MirrorTarget.allsignal_budget` ($1), capped by her max_contracts (2). The
    budget is keyed on the order's strategy, not shared state, so a main-
    strategy entry in flight cannot be sized by it.
  * Before the switch (14:15-15:45 ET), every signal was traded, 12 of 12, and
    every main-strategy window also had its $1 trade. $1 results over 10
    settled: BTC 5-0 +$1.24, GOLD 4-1 +$0.41 (fee-free).

TELEGRAM FOR THE NEW STRATEGY (operator, 16:1x ET): "clean up the alert
telegram messaging to show only the new stats for these new strategy and
execution. These current messages should be owned by the old system; for the
new system we create a cleaner version that tracks its execution and overall
and daily win rate."
  * One message per $1 trade (`messages.allsignal_trade_message`), sent at
    execution (open: side, window, signal price, fill price, both accounts)
    and EDITED IN PLACE when it settles (WON/LOST and amount). Each carries
    today's and overall record, for the instrument and for BTC+GOLD combined:
    wins-losses, win rate, P&L, fee-free.
  * The old system's messages follow the old system. With the main strategy
    paused, BTC/GOLD's signal, settlement, session and learning messages are
    quiet like any shadow's; money the old system still moves (its last exits)
    is still reported. Re-enabling main brings them back.
  * The session summary carries the new strategy only: each instrument's
    session plus today and overall. Shadow instruments are recorded, not
    sent.
  * `allsignal_trades` gained tg_message_id and reported_ms via _add_columns
    (the table already existed in the live stores).

LOCKED (operator, 2026-09-28 ~16:35 ET): "From now that strategy is locked in -
freeze everything, except when I tell you to change base size." The state
frozen:
  * BTC and GOLD. Every signal, $1 a trade, on the operator's account and the
    wife's. No gate, band or intelligence; no daily floor of its own. /auto
    stays the kill switch.
  * The main strategy is paused (shadow) on both.
  * Telegram: one message per trade, edited in place at settlement, with
    today and overall; the session summary is the new strategy only.
  * The base size (`allsignal_stake`, her `allsignal_budget`) changes ONLY on
    the operator's instruction.
tests/test_allsignal_locked.py pins the settings and fingerprints the order
path: any other edit fails the suite until it is re-approved.

ACCOUNT BALANCES IN THE SUMMARY (operator, 17:0x ET): "let the message track
account starting balance in the message and current balance". The session
summary shows each account's start and current value: cash plus open positions
at cost (`KalshiExecutionClient.account_value`). The start is REBUILT from the
broker, since Kalshi keeps no balance history (scripts/allsignal_baseline.py):
value now minus the settled P&L of every market opened since. Both accounts
start at 15:45 ET 09-28, when the $1 strategy began running ALONE on both;
from 14:15 the operator's account would have included the old strategy's last
four trades (+0.91 account-wide against the $1 book's +3.63). Stored in
btc15.db settings_text 'allsignal_start_balances': You $112.12, Wife $28.56.
The trading code (locked) is untouched; tests/test_allsignal_locked.py passes.

SETTLEMENT NOTICE (operator, 18:0x ET: "the recent three settles never fire
the telegram messages"). They did - as EDITS of each trade's message, and a
Telegram edit is silent: no notification, nothing new at the bottom. Each
settlement now also sends a short reply to the trade's message
(`allsignal_settled`). Deployed 18:15 ET. The broker showed exactly one entry
fill per market on both accounts and no exits: nothing was cashed out.

THE CASH-OUT, ON THIS BOOK TOO (operator, 18:1x ET: "cash out was never
supposed to be out - cash out must be part of the system at all levels ... it
cashes out at max profit, no need to wait for expiry"). It was never on: the
main rule (`cash_out_exit`) reads `trade_proposals`, and the $1 strategy books
in `allsignal_trades`, so every $1 trade from 14:15 rode to settlement. My
omission, not a decision.
  * `main.allsignal_cash_out` is the main rule exactly - cash_out_capture 0.90,
    cash_out_min_bid 0.90, cash_out_at_bid 0.98, all judged on the quoted bid
    minus exit_slippage 0.01; never inside the last 60s; never at or below
    the price paid; one attempt per window; crossing down to min_exit_price.
  * Through the mirroring client, so her account sells too. Gated by /auto
    only: a held position is managed to the close even with the $1 switch
    off, as the main strategy's are.
  * A full exit is graded AT THE SALE and announced then: the trade's message
    flips to "CASHED OUT +$x.xx - sold 99c with 4m00s left" and a reply says
    the same. A miss or a partial sale gets its own reply. Net figures carry
    the exit fee (`exit_fee`); capital counts only what is still held.
  * Pinned in tests/test_allsignal_locked.py (settings + fingerprint + the
    poll-loop call), at the operator's instruction.

EVIDENCE BESIDE THE DECISION (replay, read-only, 1-minute candles, the rule's
trigger minus slippage, never the candle high): 32 filled $1 trades, 14:15 to
18:15 ET. The rule would have fired on 18. All 18 won anyway; none of the 7
losers ever bid above 0.88 in a minute the rule could act, so none was saved.
Held +2.273, with cash-out +1.799: -0.474 over the afternoon (BTC -0.276, GOLD
-0.198), 2-4.6c a cash-out. At the observed best bid with no slippage it is
still -0.159. The cost is the PROPORTIONAL gate on cheap entries: bought at
0.54-0.73 it fires at a quoted 0.955-0.98, not 0.99. The 1c the operator has
seen is the absolute 0.99 trigger on expensive entries. One afternoon settles
nothing, and it agrees in sign with the config's -$0.0006/contract. Shipped on
the operator's word; the numbers are here for the next review.

THE THREAD READS IN TIME ORDER (operator, 19:0x ET: "the messaging is messed
up. This last sequence doesn't look right"). Nothing was wrong with the events
or the numbers: every message went out on time. The layout was: each result
EDITED the trade's entry message AND was sent as a reply, so every result
showed twice - once back at the entry's time (18:34 read "GOLD WON" and "BTC
CASHED OUT" before either happened) with a record frozen at the edit, which
also made the totals look out of order (29W-7L above 28W-7L). Now:
  * the entry message is never rewritten and carries no record;
  * the result - WON / LOST / CASHED OUT - is a reply to it, with today's and
    overall record, so the record only moves forward down the thread;
  * a signal whose $1 order bought nothing now says so ("NOT FILLED" /
    "ORDER FAILED"), once, recent windows only (GOLD 18:45 missed its IOC at
    0.78 and the window looked skipped).
Messaging only; the locked order path and cash-out fingerprints are unchanged.

LOOPS CLOSED (operator, 19:4x ET: "let close these loop now").
  * THE SALE PRICE COMES FROM THE BROKER. BTC 18:45's cash-out was booked and
    announced at the 0.974 quote (+$0.26); the broker filled 0.99 in three
    pieces (+$0.28). One fill read came back empty - Kalshi's fills feed lags
    the order ack - and the quote stood in. The cash-out now reads the price
    back with the entry's retries (4 reads, 1.5 s apart), and
    `Store.allsignal_reconcile_exits` confirms every exit from the synced
    fills table (exit_fee NULL = unconfirmed) and re-grades it. The existing
    18:45 row is corrected on the first sync after the deploy. The main
    strategy's record has the same fault on 45 of 241 historical exits; it is
    paused and its money figures already come from the broker's settlements.
    CASH_OUT_CODE re-pinned on the operator's word.
  * "SOME ALERTS DON'T TRIGGER AFTER THE TRADE CLOSED." Audited every $1 trade:
    since the 18:15 deploy every closed trade sent its result. The 14 trades
    from 16:15 to 17:45 have none - their results were silent edits (fixed
    18:15); GOLD 18:45 bought nothing and said nothing (fixed 19:30). Results
    also waited 60-90 s for the once-a-minute sync although graded seconds
    after the close; they are now sent wherever the trade is graded.

MIRROR ACCOUNTS FOLLOW A SWITCH PER INSTRUMENT (operator, 20:0x ET: "make the
wife mirror account or any other mirror account follow an on/off flag per
asset ... now I want it to only trade BTC"; then: m2 is Uncle George's account,
"make sure he gets the BTC trade as well").
  * `main.mirror_on(store, name)`: row mirror_<m1|m2>_enabled in each
    instrument's store, no row = on, read on every mirrored order - a switch
    takes effect on the next entry with no restart. Set with
    scripts/mirror_switch.py (--mirror wife|george --asset GOLD --on|--off;
    no arguments prints every account x instrument).
  * It gates NEW positions only (entry, add, combo). An exit still reaches an
    account that holds what this process bought for it; a switched-off account
    holding nothing of ours gets no blind reduce-only sale. A switch that
    cannot be read copies nothing new (fails closed).
  * MIRROR_INSTANCES stays the outer gate (read at startup).
  * Set now: Wife and Uncle George ON for BTC, OFF for GOLD, ETH, SOL, SILVER,
    XRP and NEAR - "only BTC" survives any instrument being switched on later.
    From 20:15 until this deploy GOLD was simply left out of MIRROR_INSTANCES.
  * Messages name who copies: the entry line "+ Wife + Uncle George", the
    summary footer "Wife copies BTC · Uncle George copies BTC", and a balance
    line per account (Uncle George starts at $10.00, 09-28 20:2x).
  * Uncle George's first copy (BTC 20:15-20:30, 20:19:12) was refused, 401
    authentication_error, on the order and the balance read. His credentials
    in .env changed after the process read them at 20:15:22: the same pair
    read afresh at 20:2x returned 200 ($10.00 on exchange index 0). A restart
    picks them up; credentials are read once, at startup.


### 111 addendum ? 2026-09-29: operator-authorized BTC-only daily profit pause

The operator explicitly requested: "APPLY THE 3% TARGET TO THE CURENT RUNING SYSTEM", then "ONLY BTC ON ALL ACCOUNT , PRIMARY AND ALL MIRRORS ... RECORD AS STARTING NOW WITH ALL THE NOW CAPITAL AS STARTING POINT". This supersedes the earlier strategy freeze for this change.

New BTC entries and adds stop per account when BTC realised profit after fees reaches 3% of that account's recorded starting capital. The hit is durable across restarts and cannot be undone by a subsequent loss. Existing exits remain available. First activation records current broker capital and starts a fresh period; later periods reset on the New York calendar day. A missing or stale broker reconciliation blocks entries. Mirrors retain their own targets; since they copy primary fills, a primary pause also prevents further copies. Gold and all other instruments have new execution disabled; research continues.

State is persisted in runtime/daily_profit.db with capture/start timestamps, opening capital, target, realised P&L, peak, pause timestamp, and notification status. V2 broker fill action is the YES-book direction, so outcome_side is the acquired leg; the guard nets opposite legs and settlement residuals without double-counting cash-outs. Telegram shows after-fee results, fresh-run statistics, opening capital, per-account target, and active/paused status. The order-path pin is updated for the newly authorized paused-result branch; cash-out behavior and $1 sizing remain pinned.

2026-09-29 follow-up: operator requested primary base budget $5 and every mirror $2, plus organized Telegram messages. ALLSIGNAL_STAKE=5, MIRROR_1_ALLSIGNAL_BUDGET=2, MIRROR_2_ALLSIGNAL_BUDGET=2; old mirror count caps removed so dollar sizing determines contract count. Entry messages, results, misses, summaries and per-account target panels show configured budgets. Existing profit-period opening capital and targets are preserved. Cash-out source pin updated only for passing the message budget. Restarted mirrors recover their held count from broker positions for reduce-only exits.

## 112. The daily 3% target, the $5/$2 all-signal, BTC only - and the incident that followed (2026-09-29)

An IDE session (operator-directed) changed the live setup at ~18:03 ET: BTC only
on the primary, the Wife's (m1) and Uncle George's (m2) accounts; GOLD and the
rest shadow; all-signal at $5 on the primary and $2 on each mirror; a 3% daily
profit target after fees per account, pausing NEW entries until 00:00 New York
(exits continue); fresh start Primary $104.77 / Wife $25.39 / Uncle George
$19.03. New module src/btc15_signal/daily_profit.py; state in
runtime/daily_profit.db.

The operator then reported: trades failing, Telegram not properly formatted, "it
keeps restarting", "a cmd that keeps opening and closing". Found and fixed:
  * TRADES FAILING: every $5 primary order was refused `insufficient_balance` -
    BTC's shard 2 held $4.38 with $98 on shard 0. The primary never auto-funded
    (only mirrors did). Moved $40 by hand at 18:55; `config.kalshi_auto_fund`
    (default True) now funds each order's shortfall on the primary too, and
    every transfer is logged ("funding [ticker]: ..."). First trade after: 19:00
    UP x6 @0.81 primary, x2 on each mirror.
  * THE FLASHING WINDOW: each watchdog's once-a-minute liveness launch of
    run_service.py ran `git` three times (revision label); git is a console
    program and, started from pythonw, got its own window - a flash every few
    seconds across seven watchdogs. revision._git now passes CREATE_NO_WINDOW;
    verified: 0 visible console windows over 70 s. Effective without restart.
  * "KEEPS RESTARTING": the IDE session's three deploys (18:03/18:05/18:13); no
    instance restarted after. The watchdog's 60 s probe is by design.
  * TELEGRAM: the IDE appended a ~12-line capital/target footer to EVERY message.
    Now (operator: "the signal should only be the trade; the result should be
    the one containing account summary and stats"; "where are the icons and
    design and table formatting"): entry = the trade (icons, no stats); result
    = outcome + stats (net, since the fresh start) + an aligned <pre> account
    table (start / today / target / status), re-read from the broker right
    before sending (2 s after a sale, so mirrors' fills are in). Failed orders
    say why. The 3% pause is announced once; its refusals are not repeated.
    The rate in every text follows `daily_profit_target_rate`.
  * DAILY TARGET: verified live - all three accounts paused at 19:25:03-04 after
    the 19:15 cash-out (You +3.34/3.14, Wife +1.33/0.76, Uncle George
    +1.33/0.57); independent recomputation from the broker matched to the cent.
    A 17-agent review confirmed and these were fixed:
      - MIDNIGHT (high): the 23:45-00:00 market settles ~6.5 s after midnight;
        its result was booked into the new day, and counted twice when the
        opening was captured after it settled. Now the opening waits for
        00:00:30 and a market that closed at or before the period start is
        never in its P&L.
      - silent fail-closed blocks: an outage is announced once (and its end);
        refused signals report "NOT FILLED - the 3% target check could not
        read Kalshi"; a failed read is retried at once, not after 15 s.
      - the monitor can no longer die silently.
      - 8 suite tests read the live .env: conftest pins RECOVERY_COMBO_ENABLED
        and the default-size test uses Settings(_env_file=None).
    Left as is (dormant, recorded): mirror adds and recovery combos do not
    consult the pause; recovery is off.
  * ADWARE on the box (Lavasoft/Adaware Browser Assistant + Web Companion,
    Defender-flagged; plus a HealthCheck{...} task starting node.exe from a GUID
    folder): 6 Run entries removed, 3 tasks disabled, processes stopped, folders
    quarantined - backups in C:\Users\admin\Quarantine_2026-09-29. The operator
    should run a Defender offline scan and rotate all Kalshi keys and the bot
    token.
  * DISK: C: had 0.21 GB free; 3.5 GB of review DB copies removed (C: 15 GB
    free). hiberfil.sys (22.4 GB) can go with `powercfg /h off` (admin).

DAILY TARGET STUDY, on the RECORDED signals and outcomes only (scripts/target_study.py,
seconds; operator: run studies on the data we collect, not long simulations).
650 BTC alerts 09-22 23:00 -> 09-29 20:00 (8 NY days, first and last partial),
won 75.8% at 0.730; sized like live, after fees, held to the recorded result.
Total after fees, per account (share of NO target):
  target      You $5/$104.77     Wife $2/$25.39    Uncle George $2/$19.03
  none         +61.87              +23.25            +23.25
  3% (live)    +17.02 (28%)        +5.12  (22%)      +8.11  (35%)
  5%           +29.99 (48%)        +8.35             +6.36
  6%           +40.92 (66%)        +11.05            +6.36
  8%           +50.52 (82%)        +12.75            +11.05
  10%          +36.21              +17.54            +12.75
  15%          +57.29              +23.22 (100%)     +18.91
  20%          +76.91              +19.84            +23.22 (100%)
After You reached 3%, the rest of those days' 446 signals still won 76% and made
+44.85 (+$0.10 a trade) - but unevenly: 09-25 +22.17, 09-27 +28.80, 09-28 -19.30,
09-29 -4.24. The target's value is days like 09-28 (up early, lost later): 8% kept
+8.94 there against -15.75 with no target. Worst day at 5-8%: -11.51 (09-26, a day
that never reached any target), at none -15.75. At $5 a single win is ~1.5-2.5% of
$105, so 3% is reached after about two wins, early in the day. Eight days, driven
by two or three of them: 10% (+36) below 8% (+51) and 20% (+77) above none (+62)
are the noise showing, not a curve to fit.

TARGETS RAISED (operator, 2026-09-29 20:1x ET: "let's move to 8% and 15% ... we can
reevaluate after 2000 signals collected ... let today continue trading"):
  * `daily_profit_target_rate` 0.08 (primary), `mirror_daily_profit_target_rate`
    0.15 (each mirror); .env set to match. The table header names each rate.
  * TODAY re-targeted in runtime/daily_profit.db at 20:14 ET (backup
    daily_profit_before_8_15.db in the session scratchpad): You $8.38, Wife $3.81,
    Uncle George $2.85; the 19:25 pause lifted (each was below its new target),
    so trading resumed from the 20:15 window. Telegram notice sent (msg 5705).
  * `main.remind_target_review`: once 2,000 BTC signals are recorded since
    09-22 23:00 ET (650 at the change), one Telegram reminder to re-run
    scripts/target_study.py on the recorded outcomes.

RULE-BASED vs ALL SIGNALS under the live targets (operator, 09-29 20:2x ET), on the
recorded signals and outcomes (scripts/rule_vs_all.py, seconds). 09-22 23:00 ->
09-29 ~20:15, 8 NY days. ALL = 652 alerts, 75.6% won at 0.730. RULE = the main
strategy's first qualifying poll per window (intelligence_decisions.base_qualified,
237 windows), 84.8% won at 0.822. Totals after fees:
                     ALL, target    RULE, target    ALL, none   RULE, none
  You  $5 / 8%          +50.52          +6.32         +52.83      +22.95
  Wife $2 / 15%         +23.22          +5.02         +20.24       +7.61
  U.George $2 / 15%     +18.91          +1.04         +20.24       +7.61
Worst day: ALL -11.51 (You) / -3.01 (mirrors); RULE -20.89 / -7.64 (09-26).
The rule wins more often but pays 9c more per contract, so a win earns less and a
loss costs more, and with ~30 signals a day against ~82 it reached the target on 4
of 8 days (ALL 6 of 8). All signals are the better base at these targets on every
account. Eight days; rule entries at the recorded quote (real main-strategy fills
ran ~1c worse); both held to the recorded result.

WORST DAYS (operator, 09-29: "what could we have done to help in those worst days"),
on the recorded signals and outcomes (scripts/worst_days.py; You, $5, 8% target):
  * 09-28 and 09-29 were good mornings and bad afternoons - the 8% target already
    caught them (+8.94 / +8.63 against -15.75 / -8.80 with no target).
  * 09-26 never got going (high +1.31, low -40.77 at no target, 72% won at ~70c
    where break-even is ~70%); ended -11.51. Losses were ordinary signals (avg 69c,
    not cheap) and short runs (max 3 in a row).
  * Protections fixed in advance, scored on ALL 8 days (live: +50.52, worst -11.51):
      daily loss stop -3% / -5% / -8%   -10.02 / +23.67 / +28.90 - HURTS (cuts
                                          days that dip then recover to target)
      pause 1 h after 2 losses in a row  +62.78, worst day +0.87 - best, but
      pause 1 h after 3 losses in a row  +25.29, worst -16.66 - so partly luck of
                                          which trades a pause skips
      give-back stop (keep 50% of peak)  +14.77 - hurts
  * Time of day: no stable pattern (adjacent hours swing +/-20); evening block
    -3.7c/signal over 156, inside noise.
  Nothing adopted. The 2-loss pause is the idea to watch, shadow-only, to the
  2,000-signal checkpoint.

SKIP-AFTER-LOSS AND A 2-MINUTE SIDE HOLD (operator, 09-29 21:xx: "skip the next signal
after a loss instead of the 1 hour pause, and the next signal must choose the side only
when the side holds for 2 minutes before entry"), on the recorded signals and polls
(scripts/skip_and_hold.py; 653 signals, 8 days; You $5 / 8%):
  every signal (live)                      +50.52  worst -11.51
  skip next after a loss                   +26.15  worst -13.89
  2-min side hold on every entry            +2.22  worst -21.28
  skip next after loss, then 2-min hold    +17.62  worst -12.60   (the proposal)
  skip + hold on all                       +14.04  worst -21.49
  (ref) 1 h pause after 2 losses           +62.78  worst  +0.87
Mirrors rank the same way. Why: the signal right after a loss won 76.6% (121/158)
against 75.5% overall - losses do not predict losses, so skipping discards ordinary
winners. The hold waited on 195 signals (30%): the price rose 69.9c -> 72.5c and the
win rate did not improve (75.4% -> 74.9%); the side flipped on only 7. Same lesson
as the timing studies: waiting pays more for the same side. And since losses carry
no streak, the 2-loss pause's +12 is almost certainly the luck of which trades it
skipped (the 3-loss version lost 25). Nothing adopted.

WHAT THE LOSING SIGNALS HAVE IN COMMON (operator, 09-29 21:xx), recorded signals only
(scripts/loser_profile.py [btc15.db|gold15.db]; facts at the alert poll, thirds,
both halves). ~30 slices tested, so only patterns that repeat on the OTHER
instrument are kept:
  * PRICE NEAR THE TARGET LINE (within ~3.5 bps): BTC won 68.8% vs 80.7% when well
    clear on the side; GOLD 65.2%, and lost money in both halves.
  * THE EARLIEST ALERTS (first qualifying moment, >= ~652 s left) win most: BTC
    81.6%, GOLD 80.0%, profitable in both halves on both. (The specific weak band
    just after it, 623-654 s, did NOT repeat on GOLD - GOLD's weak ones were later.)
  * In money: BTC ($5, 8% target) every signal +50.52; skip near-line +31.89; first
    moment only +26.27; both +11.82 - the win rate rises and the profit falls
    (BTC's near-line signals are priced cheap enough to pay, and fewer trades
    reach the target later). GOLD ($5, no target, shadow): +11.00 -> +18.33 /
    +25.96 / +33.27, worst day -22.81 -> -4.52.
  Decision: BTC unchanged (every signal). Both filters are the GOLD candidates;
  partly in-sample - confirm on new signals at the 2,000-signal checkpoint.

CORRECTION to the WORST DAYS note above: 09-28 was NOT "a good morning and a bad
afternoon". Every signal at $5 from midnight fell to -31.60 first, recovered to the
8% target only at 19:45 ET (80 trades), then fell to -15.75 by the close. When the
target is reached (You, $5, 8%): 09-23 02:15, 09-24 05:15, 09-25 02:45, 09-27 06:30,
09-29 05:00 (10-27 trades); 09-28 19:45 (80); 09-26 never (-11.51, low -40.77). The
target stops a day early on a normal day; it does NOT bound the dip before it is
reached.

MARTINGALE / STAKE PROGRESSIONS (operator, 09-29: "martingale after a loss, and on a
win reduce the size"), recorded signals (scripts/stake_progressions.py; $5 base, 8%
target, $104.77 account). 8 days: flat +50.52 (deepest drop -42.08); x2 reset
+72.20 (biggest bet $39.76, drop -58.86); x2 halve-on-win +76.68; +$5/-$5 steps
+70.24; full-recovery +87.53 (a $64.60 bet). They "won" only because the longest
run in these 8 days was 4. At 73c a win pays ~$1.54 against a ~$4.75 loss, so a
doubling does not even recover one loss. Cost of k losses in a row (73c, $104.77):
  flat: 5 -> -22.31, 8 -> -35.70 (never wiped)
  x2 (cap $40): 4 -> -74.38, 5 -> ACCOUNT WIPED
  +$5 steps: 5 -> -74.38, 6 -> WIPED
  full recovery: 3 -> -85.54, 4 -> WIPED
Chance of at least one such run (loss rate 24.5%, losses independent): 5 in a row
6% per day, 36% per week, 59% before the 2,000-signal checkpoint; 6 in a row 10%
per week, 20% by the checkpoint; 4 in a row 84% per week. Not adopted; sizing
stays flat (the operator's call - these are the numbers for it).
  x1.25 variant (operator: "multiply only by 0.25"): x1.25 after a loss, reset on a
  win +52.54; with /1.25 after a win +53.99 (flat +50.52) - +$2-3.5 in 8 days,
  inside one trade's noise; worst day -8.75 vs -11.51, deepest drop -45.5 vs -42.1.
  Streak cost at 73c: 6 in a row -54.30 (flat -26.78), 8 -97.44 (flat -35.70), 10
  wipes $104.77. Mirrors at $2 x1.25: 6 in a row ~-$20 wipes Uncle George's $19.
  Not adopted; if tried, primary only and capped at $10 (8 in a row ~-$63).

CORRECTION to "WHAT THE LOSING SIGNALS HAVE IN COMMON": observations.distance_bps is
UNSIGNED (decision.py: abs(distance)/target), and a signal's side is always the side
the price is on (UP: btc > target on all 423 alerts; DOWN: below, all 395). So
distance_bps IS the cushion on the signal's side. The "toward side" column there
multiplied it by the side's sign, so its thirds mixed UP-far / near-line / DOWN-far;
the near-line conclusion and the |distance| >= 3.5 filter used the plain value and
stand.

AFTER A LOSS, WAIT FOR A CUSHION (operator, 09-29: "wait for a better price distance
... on the next signal after the loss, study that day"; scripts/wait_after_loss_day.py,
recorded polls). 09-26, the 27 signals right after a loss: at the alert +2.80 (21 W /
6 L); wait for <=70c -2.99; wait for >=5 bps cushion +10.63 (skipped all 6 losers -
avg 1.7 bps from the line, vol 0.27 - kept 13 winners); both +2.13. All 7 full days,
after-loss signals only, no target: alert +12.15, <=70c +0.19, >=5 bps +27.09 (helped
on 09-24, -26, -28, -29, cost on 09-23, -25, -27). FULL DAYS with the live $5 / 8%
target (every other signal at the alert):
  live +43.36 | after a loss wait >=3 bps +56.97, 4 +62.76, 5 +59.40, 6 +56.10,
  8 +66.61 - every setting better, every day positive (09-26 -11.51 -> +0.4..+8.5).
The same wait on EVERY signal is unstable under the target (3 bps +13.65, 5 bps
+18.98, 8 bps +60.56). Why the after-loss form: losses stay independent, but a bad
day has more of them, so the filter engages most on bad days and seldom on good ones
(which still reach 8% fast). Designed after seeing 09-26, 7 days: in-sample.
First rule to survive a threshold sweep. Operator to choose: adopt at 4-5 bps, or
shadow to the 2,000-signal checkpoint.
  ADOPTED (operator, 09-29 22:xx ET: "adopt 5 and ship it live"): config
  `allsignal_after_loss_cushion_bps` = 5.0, `allsignal_cushion_min_left_s` = 120.
  main.allsignal_on_alert replaces the direct spawn at the alert; after a losing
  filled trade TODAY (an unknown result counts as a loss; a skipped window keeps the
  state, as in the study) it waits in ALLSIGNAL_WAIT and main.allsignal_cushion_poll,
  run on EVERY poll after primary_signal, enters at the first poll >= 5 bps clear on
  the alert's side (that moment's ask), or records 'skipped' with under 2 min left
  (Telegram "SKIPPED" with the reason; the entry says "after a loss: waited Ns for
  X bps clear"). Mirrors follow the primary's fill as always. Pinned in
  tests/test_allsignal_locked.py; tests/test_allsignal_cushion.py.

WHY A $ ORDER MISSED, AND THE FIX (operator, 2026-09-30 ~01:00 ET: "why it did not
fill", "retry 60 after check is everything still aligned", "fix the cause").
  * 00:45 window, DOWN signal 68c, IOC cap 73c: decided 00:49:54.742, Kalshi's
    created_time 00:49:57.932 - 3.2 s late; the price was running toward DOWN (cushion
    2.6 -> 6.3 bps, ask 64 -> 68 -> 82c), the <=73c offers were gone, the order was
    cancelled with 0 filled (confirmed on Kalshi; mirrors placed nothing). The recorded
    ask was back at 68c 13 s later.
  * Decision->Kalshi on every $ order: median 1.46 s before 09-29 18:03, 1.64 s after,
    worst 10.29 s; recent fills 1-4c above the signal. Each Kalshi call is only
    ~60-80 ms. CAUSE: the order runs as a background task and only advances when the
    poll loop yields; every await before its POST (the daily-target broker read, the
    shard lookup, the balance read) put it back behind the poll's other work.
  * 10 of 135 $ orders did not fill (7.4%; 3 were the 09-29 shard-balance failures);
    in 9 the recorded ask was back under the cap on the next poll (~12 s).
  * FIX: (1) the order goes out first - primary_signal and the poll yield
    (asyncio.sleep(0)) right after spawning it; (2) nothing awaits the network before
    the POST: block_reason uses the monitor's fresh figures (<30 s, no error), the
    balance is read in the background every 15 s (daily_profit._monitor_one) and
    ensure_funds spends that cache (reserving locally), and the market's shard is looked
    up at the start of its window (prewarm_order_path); (3) main.allsignal_retry_poll:
    60 s after a miss, on the first poll where everything still lines up (>= 2 min left,
    price on the signal's side, ask <= the original cap, the after-loss cushion if it
    applies), the same order is sent again; repeated every 60 s; the NOT FILLED message
    waits until no retry is possible. Pinned: RETRY_CODE.
  Pre-deploy reviews (2026-09-30 01:0x-02:3x) changed the fix before it shipped:
  - HIGH: one asyncio.sleep(0) did not send the POST - httpx needs ~7 loop turns, so
    the order still left after primary_signal's synchronous alert build. Now
    execution.ORDER_SENT/fresh_order_event is set once the entry POST is answered, and
    main.await_order_sent holds the alert on it (<= 2 s, once per attempt, only for a
    'claimed' row). Measured on a real client and socket (80 ms RTT, 400 ms build):
    POST at 449/511 ms before -> 17/105 ms after.
  - Retry fills recoverable: attempt_ms/retries columns; allsignal_reconcile searches
    around the latest attempt, only fills shaped like the entry (UP buy/yes, DOWN
    sell/no) and never for a 4xx refusal.
  - Balance cache: insufficient_balance -> clear, top up, resend once (new
    client_order_id); reservations on every path, kept across an in-flight read.
  - Prewarm: primary + mirrors, once per window in insertion order (sorted() put OCT
    before SEP and would have re-warmed every poll from 10-01), only where the $
    strategy is on. Mirror job limit 15 -> 25 s (room for a resend).
  THE REST OF THE DELAY (2026-09-30 03:xx). First live order after the 02:45 deploy
  reached Kalshi 1.10 s after the poll began - and BEFORE the alert's own work (the
  order-first fix worked). The loop's own timings (executions.timing, last 60 main
  orders) showed the rest sits BEFORE the price is read: telegram ~716 ms median every
  poll, settlement_sync ~1,246 ms once a minute (total median 1.39 s, p90 2.7, max
  5.35). A review then found most of the Telegram time is CPU, not network: a new
  httpx.AsyncClient per call loads the certificate bundle - 270-470 ms with the event
  loop blocked - on every Telegram read AND send. Fixes: Telegram reuses ONE client
  (closed on shutdown); while a $ entry is imminent (allsignal_urgent: alert pending in
  the entry range, a cushion wait, a retry due) the poll reads the price first -
  Telegram runs beside it as a background task (one reader at a time, failures
  logged; a command arriving just then applies from the next poll) and the Kalshi sync
  waits, at most 90 s.

## 113. A pause stops orders, never recording (2026-09-30)

OPERATOR (2026-09-30 ~08:00): "even if the target is hit, the shadow system should still
continue collecting data and signal an outcome of signal because that's what allow us to
do our 2000 evaluation ... Every system collecting data in the shadow still continue
collecting their data."

MEASURED FIRST (rows per hour, 09-30 00:00-03:00 trading vs 03:00-08:00 with all three
accounts paused by the daily target): every recorder on all seven instances kept its
rate - observations ~280-320/h, alerts 8/h, predictions 4/h all graded, intelligence
~100/h, shadow_decisions 4/h, $ book rows 4/h (status 'paused'), hourly ladder ~51/h,
settlement reference ~600-650/h and 4 reconciliations/h. Only 'allsignal-cashout' rows
stopped - they mark a real cash-out of a held position, and nothing was held. Of the 20
BTC signals after 03:00, 19 were usable by scripts/target_study.py (side, ask at the
alert, predictions.won); the 20th had not settled yet. The pause lives inside the order
call (execute_with_take_profit -> 'paused'), so it cannot reach a recorder.

THE AUDIT (workflow wf_067657f0-648, 20 agents, every pause/stop mechanism + adversarial
verification) found no pause that stops recording, and seven other ways recording
stopped or could stop. Fixed:
  1. MEDIUM, happening now: a poll with no usable 15-minute quote `continue`d past the
     hourly ladder (and reconciliation). Since 09-24, 204 of 211 hourly.db gaps > 90 s
     were such polls, in the last 1-3 minutes of a window; 35 of 143 chains lost their
     last snapshot. -> main.record_shadows on that path.
  2. MEDIUM: the poll loop caught only network-shaped errors. A bug (TypeError,
     NameError, sqlite) ended the process, and after 7 exits an hour the watchdog
     returned for good - every recorder on the instance down until someone noticed.
     -> main.report_cycle_error: the poll carries on; traceback once per error per
     15-minute window; Telegram "SOFTWARE ERROR" once per error per New York day, from
     every instance. The trading block has its own try, so the recorders and exits
     behind it still run in the same poll.
  3. The settlement sweep ran before any recording and was unguarded: one market whose
     result could not be read aborted every poll. -> each read guarded, sweep in its own try.
  4. hourly_shadow promised "never raises" but let sqlite errors out -> catches Exception.
  5. DailyProfitGuard.block_reason raised on a locked daily_profit.db -> fails CLOSED.
  6. Gold/silver slept a flat 600 s through the weekend closure, waking up to 10 min
     after the reopen (~10% of reopens would lose the first window's alert and
     prediction) -> sleeps to reopen + 30 s at most.
  7. Settlement reconciliation read one page of 50 markets: an outage > 12.5 h lost the
     rows before it for good -> pages back (200/page, at most 4) to a reconciled market.
  Watchdog: past 6 restarts an hour it now says so once and retries every 15 minutes;
  a run of >= 10 minutes resets it. It never stops for good.
  Not changed, on purpose: decision_records stays empty while the main strategy is
  paused - it is the order path's own audit; the rule's shadow (verdict, band hold,
  graded outcome) is in intelligence_decisions, which rule_vs_all.py reads.
  scripts/strategy_switch.py's docstring corrected; main_strategy_on's (which says "it
  still records every decision") is inside the LOCKED fingerprint, so it stays as is and
  this note is the correction. A kill in the <= 2 s between an alert's claim and its
  predictions row would lose that row (0 of 3,269 windows so far); restarts are made at
  window opens, where no alert can be in flight.
  Tests: tests/test_recording_never_pauses.py.
  PRE-DEPLOY REVIEW (wf_f4f9d41f-773, 15 agents) changed four things before it shipped:
  - MEDIUM: surviving errors dropped a failed /auto off. updates() moves the offset past
    the batch before handlers run; the old crash-and-restart was what re-read it. Now a
    failed COMMAND rewinds the reader (read again next poll, 3 tries, then "COMMAND
    FAILED"); a failed BUTTON is never replayed (a replayed Execute could order twice).
  - MEDIUM: the error alert went through btc15.db (claim, one attempt): a DB fault or one
    failed send meant no Telegram at all, where the old crash path alerted 7 times via
    the watchdog. Now it sends directly, retries a failed send after 5 min, says what did
    not run (orders / settlements / rest of the poll), and repeats hourly with a count
    while the error recurs.
  - MEDIUM: waking at reopen+30 s landed inside Kalshi's listing delay (gold median 34 s,
    p90 48 s, n=303); with the gap still timed from Friday the first poll re-closed for
    600 s or raised a false 48 h NO MARKET alert (simulated: misses 16% -> 44%). Now a
    passed reopen ends the closure (main.end_closure_at_reopen) and the loop polls
    normally until the market lists.
  - LOW: the watchdog's backoff flag survived a hand-started service, so a later failure
    episode was silent. A held lock now resets it.
  A second check of those fixes (wf_c5817852-bbf) added: failure texts never carry the
  bot token (an httpx error's text includes the URL); only an alerting instance repeats
  an error hourly, shadows once a New York day (one shared-code bug would otherwise be
  ~168 messages a day); only a real closure (>= 30 min) is ended at its reopen; the
  watchdog retries an undelivered KEEPS FAILING and survives a failed process launch.
  The watchdogs keep their code in memory: this deploy restarts them too
  (RUNBOOK updated). Every LOCKED and trading function is byte-identical to before.
  2,000 SIGNALS: 698 on 09-30 at ~95 a day -> about 10-14, not within a week.

## 114. A 5% target per SESSION instead of per day: worse on every account (2026-09-30)

OPERATOR: "Now test 5% target per session let see". Measured on the recorded signals
and outcomes only (scripts/session_target_study.py; same inputs, sizing and fixed account
sizes as scripts/target_study.py): 703 BTC signals, 09-22 to 09-30, 9 NY days, 31
sessions (asia 20-03 ET, europe 03-09, us 09-17, late-us 17-20). Held to the result, no
cash-out, no cushion.

  9-day total          no target   daily (live)   5%/session   (3%..8%/session)
  You  ($5, $104.77)     +72.88     +60.22 (8%)     +18.13      -23.43 .. +63.61
  Wife ($2, $25.39)      +29.31     +27.29 (15%)     -3.86       -7.26 .. +8.03
  George ($2, $19.03)    +29.31     +21.98 (15%)     -5.06       -8.85 .. +3.42
  Worst day, You: -35.47 per session vs -11.51 daily; losing days 2 vs 1.

WHY: a session target cuts a WINNING session short (09-28 US: +5.42 taken, +17.56 there)
but lets a LOSING session run to its end (09-28 asia -16.76, 09-29 US -28.41). Four
resets a day are four chances to meet a bad run; the daily target is hit early on most
days (7 of 9) and then sits out the rest, bad runs included. 09-28 and 09-29 carry most
of the gap (You -35.47/-21.55 per session vs +8.94/+8.63 daily); per session was ahead
on 4 of 9 days. Every rate from 3% to 8% per session is below the live daily rule on
all three accounts. n = 9 days - small, but not close.

## 115. After the target on 09-30: the rest of the morning won (one day)

OPERATOR: "After we had reached our target how did the market do up to now". Recorded
signals and outcomes after each account's own pause (scripts/after_target_day.py
2026-09-30, at 11:19 ET; held to the result, no cash-out, no cushion): 33 BTC signals
after 03:00, 27 won (82%, against 76% over the 703-signal record). Kept trading, You
would be +20.55 instead of +9.33 (+11.22; path never below the pause point, peak +17.33
at 07:00, then -14 over 07:00-09:59 before recovering); Wife +8.35 instead of +4.03;
Uncle George (paused 02:28) +8.42 instead of +3.16. One day, and not over: the 9-day
study (FINDINGS 112/114) has the same target costing on days like this and saving on
09-28/09-29. The decision stays with the 2,000-signal review; rerun the script daily
to build the after-target record.
  COMPARED WITH 09-29 (scripts/day_compare.py 2026-09-29 2026-09-30, at 11:22): same hours
  00:00-11:59, 09-29 48 signals 79% +8.39 vs 09-30 45 signals 84% +23.76 (You, $5). 09-29
  as a whole: 96 signals, 75%, -5.48; an 8% target from midnight would have been hit at
  05:00 (+8.63) and sat out -14.11. Both mornings dipped 07:00/08:00-09:59.
  HOUR OF DAY, A HYPOTHESIS ONLY (711 signals, 9 days, ~30 a bucket): 08:00 ET 62.5% won,
  -25.75, losing on 6/8 days; 09:00 -13.39; 15:00 -24.13; 20:00 55.6%, -35.64, 5/7 days.
  Plausible cause - the 08:30 US data, 09:30 equity open, 20:00 Asia open - but 24 buckets
  of ~30 signals will show a couple this bad by chance (20:00 is about -2.5 SE, 08:00
  about -1.8). Test at 2,000 signals with a paired skip-vs-trade comparison before any
  rule; do not pick hours from their own buckets.

## 116. After the daily target: drop to a lower base instead of pausing (2026-09-30)

OPERATOR: "after we hit the 8% at $5 base then we move to lower base for all remaining
of the day at $2 base until midnight and back to $5 again. Test that over all the 9
days." Then: "did you put in play the new implementation 5 bps required after a loss" -
the first run did NOT (held every signal at its alert); the rerun below does, exactly as
main.allsignal_on_alert/allsignal_cushion_poll (scripts/target_rules_live.py; 0 = off).
712 BTC signals 09-22 23:00 to 09-30 11:15, fixed capital as target_study.py, held to
the result; mirrors copy the primary's taken signals, as live. Recomputed independently
to the cent (workflow wf_6babaa5d-2bd, own script, did not read ours).

  WITH the 5 bps cushion   You $5    worst   losing   Wife     George
  no target                +98.29    -6.12     1      +35.16   +35.16
  pause at target (live)   +76.26    +0.48     0      +24.94   +22.71
  $2 after 8% ($1 after 15% mirrors)  +85.93  +0.48  0  +31.98   +28.69
  WITHOUT the cushion: no target +80.92, pause +60.22, $2 after +69.30 (You).

  $2-after beat pausing on 5 of 8 target days; the $2 part gave back on 09-28 (-6.15 vs
  pause) and 09-24 (-0.38). Under it, 190 signals came after a loss: 109 entered at the
  alert, 41 after waiting, 40 skipped.
  CAUTION: the 5 bps cushion was chosen on these same days (FINDINGS 112), so every
  with-cushion row is in-sample and flattering; the pause-vs-$2 comparison shares it and
  is fairer. 9 days (09-22 is one hour). Operator's decision; confirm at 2,000 signals.
  Mirror pause figures are lower than the independent ones given first (27.29/21.98):
  live, a mirror stops when the primary pauses.
  ROBUSTNESS (method review wf_ddf2a224-3fd, recomputed independently to the cent):
  - The ranking for You - no target > $2 after > pause - held in every variant: cushion
    on/off, fixed vs compounding capital, targets 7-10%, full days only. It flips only
    if live execution costs >= ~$0.015 per contract (measured live gap ~$0.0045).
  - The SIZE of the gaps does not hold: on the 7 full days (09-23..09-29, no cushion)
    You is +48.98 / +43.36 / +46.84 (none / pause / $2 after); $2-after minus pause
    averages +1.25 a day with SE 2.31 - inside the noise.
  - Hits are thin: 4 of 7 crossed the target by $0.56 or less (09-29 by $0.25). 09-28
    crossed at its peak (+8.94 vs 8.38) - trim $0.56 and pause loses 24.69 there.
    Live execution (cash-outs at 0.98-0.997, 7% unfilled, fills above the ask) moves
    hit times: 09-30 replay crossed at 02:15, live at 03:00.
  - allsignal_trades.pnl is BEFORE fees: 09-30 book 10.265 - 0.935 fees = broker 9.3303.
  NO TARGET, CUSHION ONLY (operator: "what about the no target but requiring 5bp after a
  lose"): You +98.29 (7 full days +67.15), 1 losing day (09-28 -6.12); Wife/George +35.16.
  Its cost is depth, not days: the deepest drop from a high is -40.32 (~38% of a $105
  account, 09-27 peak to 09-28 low; 09-28 fell to -26.94 intraday), against -26.94 for
  pause and -31.71 for $2-after. Every with-cushion figure is in-sample (FINDINGS 112).

## 117. DECISION: primary $6 base, $3 after its 8% target until midnight (2026-09-30)

OPERATOR: "Make primary account base size 6 and apply $3 after the 8% target hit only to
mine the primary; the mirrors stay at the pause when hit target, so only these to
primary, the others continue as usual."
IMPLEMENTED: .env ALLSIGNAL_STAKE 5 -> 6, ALLSIGNAL_AFTER_TARGET_STAKE=3 (config default 0
= the old pause). main.allsignal_stake_now sizes each new $ entry: $6, or $3 once the
PRIMARY's guard has reached today's target, until 00:00 New York; the stake is recorded
on the allsignal_trades row (new column `stake`) and every message names it. The primary
guard alone has after_target_stake=3: past its target it no longer blocks, but unknown
figures still do. Mirrors unchanged: $2 each by their own budgets, paused by their own
15% targets. BEHAVIOUR CHANGE FOR THE MIRRORS: they no longer stop when the primary
reaches its target first - they copy its $3-phase fills until their OWN 15%.
LOCKED_CODE and CASH_OUT_CODE re-pinned on this instruction (tests/test_allsignal_locked.py).
Tests: tests/test_after_target_stake.py.
EVIDENCE BESIDE IT (scripts/target_rules_live.py 5 6 3 115.39: recorded signals and
lifecycle, 5 bps cushion as live, 713 signals 09-22 23:00 to 09-30, held to the result):
  You: $6 then $3 +101.69 (1 losing day, 09-26 -1.27) vs pause at $6 +78.75 vs no target
  +128.29 (2 losing days, worst -5.99). Mirrors as live now (pause at 15%, copying until
  then): Wife +29.19, George +23.28 (vs +21.47 / +20.84 when they stopped with the primary).
  Same cautions as 116: the cushion is in-sample; the gap to pausing is within the noise
  on 7 full days; hits are thin. Review at 2,000 signals.
  PRE-DEPLOY REVIEW (wf_2f6943e5-bac, 11 agents; all findings low) changed it before it
  shipped:
  - Only the $ strategy passes the primary's target: block_reason(ticker, strategy) lets
    strategy "allsignal" through at $3; the main strategy, recovery adds and manual presses
    still pause at the target, as before (they would otherwise have traded at full size).
  - The ENTRY'S SIZE decides past the target (second check, wf_2d16f49d-79c): any $ entry
    larger than $3 is refused - whatever the timing - and announced as skipped; a retry
    after the target is resized to $3 (RETRY_CODE re-pinned).
  - The notice has its own state ('lowered'), so today's switch is announced once.
  - The accounts table says NO DATA before HIT $3 when figures are stale (entries blocked).
  - SKIPPED labels and the session summary name the lower stake; LOCKED_CODE now also
    hashes main.allsignal_stake_now (the rule that sets the live size).
  Known, harmless: prewarm pre-funds $6 after the target (moves cash between shards of
  the same account only).
  WATCHDOG, same deploy (operator 12:36: "I see nothing in telegram all are just the old
  restart"): a 10:50 "SERVICE RESTARTED - died on startup after 0 min" came from ONE
  failed once-a-minute check (it imports the whole app before it finds the lock held);
  no service stopped (PIDs unchanged since 09:30, no restart in any log). Its output was
  discarded, so the cause is unknown. Now a check that fails while the service holds its
  lock is logged, not reported as a restart (Telegram only at 10 in a row); every check
  failure and restart is kept in runtime*/watchdog.log with the output; messages name
  the instance.
  MESSAGES (operator 13:0x: "should be specific about what is paused"): the accounts
  table under every result names who is paused and who continues - "Primary · target
  hit · $3 per signal until 00:00 ET, then back to $6" / "Wife and Uncle George · target
  hit · no new BTC entries until 00:00 ET" / "<name> · Kalshi figures unavailable".

## 118. The 5 bps cushion live, first day (2026-09-30, verified wf_234f8d7c-e2d)

OPERATOR: "Now check how it has been doing and how the 5bp help out?" (scripts/
cushion_live_review.py 2026-09-30, recomputed independently to the cent.)
MONEY (broker settlements = book, net of fees): primary +13.83 on 106.05 (+13.0%): +9.33
at the $5 phase (10W-1L, target at 03:00), +4.50 at $3 since 12:45 (13 trades, 11W-2L;
holding to settlement would have been +4.71 - cash-outs cost 0.21). Mirrors paused all day
at +4.03 / +3.16.
CUSHION LIVE: 3 after-loss signals, no skips. 00:30 already 5.92 bps clear -> entered at
the alert (0.00); 13:00 waited 34 s (2.87 -> 5.10 bps), +1.12 vs the alert - +0.62 from the
wait (66c -> 60c, one more contract), +0.50 from a fill at 50c against an 11 s stale 60c
recorded ask; 15:00 waited 11 s, -0.16 - fill slippage (70c ask, 74c fill), not the wait.
Net +0.96. n = 3: no conclusion.
REPLAY, every signal of the day at $6 (recorded lifecycle): +39.07 without, +42.30 with
(+3.24); 11 after a loss: 6 at the alert, 3 waited (04:00 -0.96, 13:00 +0.91, 15:00 0),
2 skipped (12:45 loser +5.72, 12:30 winner -2.44). One skipped loser carries the day.

## 119. A second target at the lower stake: close the day at 8% + 8% (2026-09-30)

OPERATOR: "after the 8% on the $6 ... then on the $3 we can close the day at another 8%
... 16% target for the day". scripts/target_rules_live.py 5 6 3 115.39 (rule lower_stop,
SECOND_RATE 0.08), recorded signals + lifecycle, live cushion, mirrors as live; 735
signals; recomputed independently to the cent (wf_a80be1cd-999).
  You: close at 16% +105.27 vs $3 to midnight (live) +103.26 vs pause at 8% +78.75 vs no
  target +132.55; worst day -1.27 for all three target rules. Mirrors unchanged (+29.19 /
  +23.28): on every closing day both had already paused at 15%.
  The second 8% ($9.23 from $3 trades) was reached on 3 of 9 days: 09-23 closed 10:30, the
  rest of the day would have lost 6.99 (close helped); 09-27 closed 11:15, the rest won
  +3.21; 09-30 closed 06:30 in the replay, the rest +1.77 by 17:00. Net +2.01 - noise.
  Not adopted (operator's call); the live rule stays $3 to midnight.

## 120. DECISION: the primary's stake follows the day against its target (2026-09-30 17:5x)

OPERATOR (after a $3 afternoon took the day from +13.83 back to +4.67, under the 8.48
target): "invalidate the daily target hit and trade size back to the $6 and then continue
the cycle until the target is hit and then back to the $3 until midnight and then reset."
TESTED FIRST (scripts/target_rules_live.py 5 6 3 115.39, rule 'follow'; 737 signals,
live cushion, mirrors as live; recomputed independently to the cent, wf_7be87c25-008):
  You: follow +118.20 vs $3-to-midnight (live) +97.87 vs close at 16% +105.27 vs pause
  +78.75 vs no target +120.94. Worst day -1.27 (as the other target rules), 1 losing day;
  beat the live rule on 09-23/24/25/29, behind on 09-28 (+1.04 vs +3.20). 24 flips to $3,
  18 back to $6 (42 signals at $6 after the target had been reached). Mirrors unchanged
  (+29.19 / +23.28). Cushion OFF: follow +102.04 vs live +82.22. In-sample; 9 days.
IMPLEMENTED: main.allsignal_stake_now = $3 while the primary's realised day P&L >= its
target, else $6; DailyProfitGuard.block_reason's size gate applies only while at/above it
(other strategies still pause once the target was reached); notices on each change of
side ("TARGET REACHED [AGAIN]" / "BACK BELOW TARGET ... Back to $6"), table HIT $3 /
BACK $6. Mirrors: unchanged. LOCKED_CODE re-pinned.

## 121. A third mirror account (2026-09-30 ~19:xx)

OPERATOR: "I added MIRROR 3 enable it to trade same as the other MIRRORs". The code read
only m1/m2: config gains mirror_3_* (same fields; label "Mirror 3" until MIRROR_3_LABEL
names it), mirror.MIRROR_SLOTS = (1, 2, 3), scripts/mirror_switch.py knows m3. Same
treatment as the others: $2 per BTC signal (MIRROR_3_ALLSIGNAL_BUDGET), copies the
primary's fills, own 15% daily target, auto-funds shard 2 from shard 0, recovery off,
per-asset switch ON for BTC and OFF for ETH/SOL/XRP/NEAR/GOLD/SILVER (as Wife and Uncle
George). Read-only check before going live: key works, $10.18 ($8.99 shard 0, $1.19
shard 2), nothing open. Its first day's target starts at activation (15% of ~$10.18).
Tests: tests/test_mirror_three.py.

## 122. A safeguard that locks the day on returning to the target: worse (2026-09-30 ~21:xx)

OPERATOR: "when we hit the 8% and at $3 and then we go back down in loss pnl for the day
we trigger a safeguard that will pause trading once we hit the 8% back again." Two
readings tested on top of the live follow rule (scripts/target_rules_live.py 5 6 3 115.39,
rules guard_below / guard_loss; 750 signals to 09-30 21:00, live cushion; recomputed
independently to the cent, wf_f48bdd89-a02):
  follow (live) +117.64 | A: armed by falling back below 8%, locks at the next 8% +96.97
  (locked 5 days; the rest of those days would have made +4.25, +8.54, +10.18, +6.89 and
  -9.19 on 09-28 - the only day it helped) | B: armed only by going below $0, locks at the
  next 8% +109.67 (fired once, 09-25: fell to -0.71 at 10:00, closed 11:30 at +11.71; the
  rest of the day made +7.97) | no target +119.93. Worst day -1.27 for all three.
  On this record a return to the target after a dip has usually been followed by more
  profit. Not adopted; recheck at 2,000 signals.

## 123. Mirror targets scale with the account: % of opening, capped at 7 wins (2026-10-01)

OPERATOR: Wife at $2 peaked +4.43 against a 4.56 target (15% of 30.40) on 10-01 and gave
it back, while Uncle George (3.47) and Affoue (1.22) locked theirs; "keep her at $3, just
design a scale mechanic that auto adjusts the % based on account growth."
WHAT DECIDES THE HIT RATE is the target in WINS at the stake, not the %: a win = what the
stake makes at a typical 75c entry (contracts_for_budget(stake, 0.75) x 0.25 = $0.50 at
$2, $1.00 at $3 - $3 buys 4 contracts at 65-75c, twice $2's 2). Measured on the mirrors'
recorded copies 09-23..10-01 (follow rule, cushion; scratchpad wins_sweep.py):
  $2: 4-9 wins hit 8 of 9 days (+16.48..+35.05); 10 wins 6/9; 12 wins 5/9.
  $3: 4-7 wins hit 8 of 9 (+36.16..+58.30); 8 wins 7/9; 9 wins 6/9.
  Wife at $2 needed 9.1 wins - the edge; at $3 she needs 4.6.
IMPLEMENTED: a mirror's day target = rate x opening, but never more than
MIRROR_TARGET_MAX_WINS = 7 wins at its stake (DailyProfitGuard.day_target, at the
00:00:30 capture). As an account grows the % falls and the target stays reachable: Wife
$3 caps at $7.00 (15% until $46.67, 11.7% at $60); George and Affoue $2 cap at $3.50 (15%
until $23.33). The primary has no cap. The table header and the DAILY TARGET ACTIVE notice
show each account's effective %. First applies at the 10-02 opening. In-sample; 9 days.

## 124. BNB: an eighth instance, SHADOW ONLY (2026-10-01)

OPERATOR: "In the shadow tracking signal and life circle let add BNB". Kalshi lists
KXBNB15M ("BNB 15 Minute", fifteen_min, CF Benchmarks, CRYPTO.pdf terms) - the same
structure as NEAR. The reference comes from Kalshi's /live_data/events/{event}, which is
generic, so nothing BNB-specific was needed there.
ADDED: surface.asset knows BNB (the corpus guard depends on it - FINDINGS on NEAR);
shadow_summary.STORES BNB -> bnb15.db; learning_runner.SLOT_ORDER gains BNB (slot 7 of 8);
scripts/run_bnb.ps1 (own runtime-bnb/, reference and policy paths, AUTO_TRADE_ENABLED false,
LEARNING_ENABLED false - no BNB corpus yet, the corpus paths name BNB's own future files);
strategy_kalshi_bnb.json = SOL's COVERAGE band copied (0.55-0.90 asks, 0.5-12 bp), labelled
as not fitted on BNB - it only decides what is marked rule-qualified; alerts, lifecycle and
outcomes are recorded either way. bnb15.db created with auto, main, $ and m1/m2/m3 mirror
switches stored OFF before the first start; BNB is in neither ALLSIGNAL_INSTRUMENTS nor
MIRROR_INSTANCES, and TELEGRAM_ALERT_INSTRUMENTS=BTC mutes its alerts. Startup launcher
BTC15Signal-BNB.cmd installed. scripts/full_circle_check.py and the instrument tests cover
it. Refit its config on its own record after a week or more.

## 125. The live rules on ETH, SOL and GOLD (2026-10-01, verified wf_75a528cb-1f4)

OPERATOR: "under the current live rules system how would ETH, GOLD and SOL have performed
on those 9 days". STUDY_DB=<db> scripts/target_rules_live.py 5 6 3 115.39 (each
instrument's own recorded signals and lifecycle; $6 below the 8% target, $3 at/above;
cushion as live; held to the result); recomputed independently to the cent.
  BTC  (10 days)  live rules +128.18 (cushion off +116.25), worst -1.27, 1 losing day
  ETH  ( 8 days)  +38.61 (off +44.76), worst -30.93 (09-28), 2 losing days
  SOL  ( 7 days)  +30.81 (off +14.48), worst -66.76 (09-28), 2 losing days
  GOLD ( 7 days)  -27.84 (off -16.68), worst -28.97, 4 losing days (09-28..09-30 in a row)
09-28 hit ETH, SOL and GOLD hard while BTC made +1.04: the target only lowers the stake
on the way UP; below it a bad day keeps betting $6 with no daily loss stop. The 5 bps
cushion (fitted on BTC) helped SOL, hurt ETH and GOLD. BTC-only stays; ETH/SOL would need
a daily loss stop before going live; not GOLD under these rules. 7-8 days each.

## 126. A daily LOSS stop on top of the live rules, per instrument (2026-10-01)

OPERATOR: "what could a daily stop loss look like and how it would help them".
scripts/loss_stop_sweep.py (target_rules_live.py rule follow_stop, STOP_RATE): live rules
($6 below 8%, $3 at/above, 5 bps cushion) + no new entry once the day is down X% of 115.39;
recomputed independently to the cent (wf_306ca78c-5f8, freeze 10-01 12:00).
              none      -5%      -8%     -10%     -15%     -20%
  BTC     +128.34   +58.52  +101.99  +101.99   +88.86  +100.95
  ETH      +39.23    +1.65    +1.86    -3.96    +7.66    -0.02
  SOL      +30.99    +7.50   +69.36   +62.98   +50.49   +40.37
  GOLD     -24.65   +32.69   +20.06   +20.06    -0.84   -21.23
  SOL's 09-28 -66.76 -> -10.27 at -8%; GOLD's 09-28..09-30 ~-27 each -> ~-12.
SOL and GOLD lose in long one-way runs, so a stop caps them (-8..-10% works for both);
BTC and ETH dips usually recover the same day, so a stop locks losses in (as for BTC in
FINDINGS 112). The day can end past the stop: it is checked after the trade that crosses
it settles (BTC/ETH reach -14.59 at -8%). If SOL or GOLD ever go live, pair them with a
-8..-10% daily stop; none for BTC. 7-8 days each. Nothing changed live.
  CORRECTION (same day): listing BNB as an EIGHTH name in learning_runner.SLOT_ORDER filled
  the 8-window cycle, so an UNLISTED instrument got the unreachable slot 8 and
  hypotheses_delay_s looped forever - the full suite hung at test 1,033 twice (my
  30/60-minute limits read it as a timeout). No live effect: every running instance is
  listed and none had loaded the change. BNB is unlisted again (it takes the spare slot 7;
  its learning is off), and hypotheses_slot now always returns a reachable slot.
  LIVE: BNB started 2026-10-01 13:40 under its watchdog after the full suite (1904) passed;
  connected to KXBNB15M-26OCT011345-45; first signal 13:49:31 (DOWN, reference 773.23),
  lifecycle, intelligence and reference rows writing (BRTI-equivalent 54/57 ok, as NEAR);
  $ book empty, auto/main/$ switches off.

## 127. WHAT THE FIRST 10 DAYS ESTABLISH (2026-10-01, workflow wf_e414dad7-a30, 4 angles)

OPERATOR: "Base on the current 9 days and our live strategy, what can we establish about
our system performance, before we get the 2000 trades?" Direct arithmetic on 804-805 BTC
signals (09-22 23:00 .. 10-01 ~14:30) and the live book; scripts in scratchpad/assess/.
ESTABLISHED
- The signal beats Kalshi's price BEFORE fees: won 76.2% at an average ask of 0.733, edge
  +3.0 pts (p 0.01-0.03 on every test), the same for UP and DOWN.
- Execution delivers the signal: fills average 0.05c better than the ask; book = broker to
  the cent on 216 entries, 104 exits and every daily figure; fee model exact; fill rate
  94-97% (retry recovered 5 of 8 misses, all winners).
- Losses do not cluster (after a loss 77.8% won vs 76.1% after a win; longest losing run 3).
- The target rules change the SHAPE of returns, not the expected value: daily SD 8.8 vs 13.2
  with no target, same money; they do not reduce intraday dips (09-28 -31.8, 09-26 -21.7).
NOT ESTABLISHED
- Profit AFTER fees: +1.6c/contract, 95% CI [-1.2, +4.5]; the fee takes ~45% of the gross
  edge. 2,000 signals (~1,190 after the current rules) give only ~35% power; ~3,700
  post-rule signals for 80%.
- Every fitted rule (5 bps cushion, 8% target, follow $6/$3, 15% mirror target, 7-win cap,
  no BTC loss stop): per-signal effects indistinguishable from zero; dollar advantages come
  from 1-2 days (09-26, 09-28). Cushion out of sample so far: -1.18 replay, -3.05 live
  (waits pay ~8c more). 8% ranked 4 of 10 at the decision, below the median today.
- Leads only (picked after looking): asks 0.65-0.70 (39% of signals) show no edge and lose
  ~1.9c after fees; asks >= 0.70 carry it (+4.1c). Hours 08/09/15/20 ET weak. Not rules.
RISKS FOUND
- Mirrors: no downside cap, full stake on the bad days. Wife at $3 fell ~49% intraday on 09-28
  (replay); Affoue at $2 on $8.10 is wiped out by a 09-28 morning (cash-limited -89%).
- Cash-out costs money so far: -$4.28 over 104, all winners sold, no loser saved.
- REPORTING GAP: a held 23:45 position settles at 00:00:06 and lands in NEITHER day's
  daily_profit figure (09-30 showed +8.08; the real day by window was +2.26, -5.81 hidden).
PRE-REGISTERED 2,000-signal checklist T1-T10 (signals from 10-02 00:00 only): base edge,
cushion (with an after-win control), follow vs $3-to-midnight, target vs none (money, risk,
mechanism), target rate, mirrors, BTC loss stop, cash-out, execution, parked hypotheses -
see the overfit agent's output (wf_e414dad7-a30 journal).

## 128. The midnight count gap, closed (2026-10-01)

OPERATOR: "Address the midnight count gap." A position held to settlement in the 23:45
window settles ~00:00:06 - after the old day's last refresh - and the new day leaves it
out on purpose (its result is already in the new opening, read at 00:00:30). So it was
counted in NEITHER day. FIX: DailyProfitGuard.close_out - for the first 30 minutes of a
day, each refresh recomputes the PREVIOUS day from scratch from the same broker events:
every event from its start to now for markets that closed after its start and by midnight.
Idempotent; never touches the new day; prints when it changes a figure. Tests:
tests/test_daily_profit_closeout.py. BACKFILLED (scripts/backfill_day_closeout.py, broker
reads only, past rows only; copy of the DB kept): 09-30 You +8.08 -> +2.26 (the -5.81 23:45
loss), Affoue -0.42 -> -2.08 (her copy of it); Wife, Uncle George and every 09-29 figure
unchanged (paused / cashed out before midnight). Affoue is being funded to ~$100 on 10-01:
from 10-02 her target is the 7-win cap $3.50 (~3.5%), and she is ~50 stakes deep.

## 129. The mirrors' STAKE scales with the balance at midnight - 2% (2026-10-01)

OPERATOR: "I thought you had the auto scale for the mirrored account as the account
balance changes every day at midnight but a nice safe scale. Only my primary is
controlled manually on aggressive." Until now only the mirrors' TARGET % scaled (123);
their stakes were fixed in .env. IMPLEMENTED: at each 00:00:30 opening a mirror's $
stake for the day = 2% of its opening in whole dollars (MIRROR_STAKE_SCALE_RATE=0.02),
never below its own MIRROR_n_ALLSIGNAL_BUDGET (the operator's stake stays the floor:
Wife $3, George $2, Affoue $2) and never above $6 (MIRROR_STAKE_SCALE_MAX, the primary's
base). Stored in profit_days.stake (new column, migrated); the 7-win target cap uses it;
DailyProfitGuard.sync_stake points the mirror's MirrorTarget at it every refresh, so the
copier and the pre-funding size by it with no change to the LOCKED code. The midnight
"DAILY TARGET ACTIVE" notice says each mirror's stake. The primary is untouched.
WHY 2%, NOT 3% (scripts/mirror_stake_scale_study.py; recomputed independently, every
number matched): the live rule took 764 of 808 recorded signals, 593 won (77.6%) vs a
75.5% break-even - 1.4 standard errors, real money so far, not yet proven. Worst seen:
3 losses in a row, ~4.3-4.9 stakes from a high. If the edge were zero, a 1-in-44 day is
~11 stakes: 22% of the account at 2%, 33% at 3% (the primary's 5.6% would be ~60%).
2% = about a quarter of the measured Kelly fraction. Steps: $2 below $150, $3 from $150,
$4 from $200, $5 from $250, $6 from $300. Nobody's stake changes on 10-02 (Affoue ~$100
-> $2; Wife and George are at their floors). TENSION, SAID: at their set stakes Wife
($3 on ~$35, ~9%) and George ($2 on ~$27, ~7%) risk MORE per signal than the safe rate
and than the primary; the scale only raises. Lowering them is the operator's word only.
The target pause limits nothing on a losing day (it stops a mirror only when it is up).
REVIEW BEFORE DEPLOY (two independent passes; every claim checked against the code):
- HIGH, FIXED: MirrorTarget is a FROZEN dataclass, so the first version's assignment of
  the new stake raised, the error was swallowed, and every copy would have stayed at the
  floor while the target and the Telegram notice used the scaled stake. All its tests
  passed because they used a stand-in object. Now the guard holds the live mirror and
  swaps in a copy of its target; tests drive the real MirrorTarget and _Mirror._apply,
  and fail when the bug is put back (3 of 10).
- FIXED: the stake stored at midnight overrode the operator's mid-day change (as Wife's
  $2 -> $3 on 10-01) and the off switch. The day's stake is now the opening under the
  CURRENT settings; the stored column is a record.
- FIXED (close-out, 128): the broker read reached back a flat 24 h, so on 11-02 the
  25-hour DST day 11-01 would have been rewritten without its first hour. It now reads
  from the previous New York midnight.
- Hardening: a rate above 5% or no ceiling scales nothing (the operator's stake).
OPERATOR DECISION (2026-10-01, evening): "All mirror accounts will be topped up so let
them grow and catch up to the scale we have set for them." The floors STAY (Wife $3,
George $2, Affoue $2); nobody is lowered. Top-ups bring each account up to where its set
stake IS the 2% scale (Wife $150, George and Affoue $100), and the scale raises it from
there. The opening counts ALL cash (every shard) plus open positions at cost, so a top-up
landing before 00:00:30 ET sets the next day's stake and target; one after it waits a day.
SECOND REVIEW PASS (both confirmed, both FIXED):
- After a mid-day settings change + restart the stake followed but the stored target (its
  7-win cap) did not: lowered from $6 to $2, a $14 target is 28 wins - no pause that day.
  Now a mirror's target and stake are re-derived from the opening under the current
  settings until the day pauses (latched), and a moved target is announced again. The
  primary's record is never re-derived.
- close_out ran only 00:00-00:30; an outage across it left the 23:45 settlement in
  neither day again. It now runs all through the new day (idempotent).
Mutation-checked: removing either fix fails a test.

## 130. Signals below 70c: a lower win rate, priced in - and a floor would have cost money (2026-10-01)

OPERATOR: "in our analysis earlier we found that signal below 70 cent had low win?" The
earlier result (98) was COMBO PARTNERS priced below 0.70 (27-38% combos won). On the $
strategy itself (scripts/entry_price_bands.py; recomputed independently, it matched):
767 live-rule entries 09-22..10-01, net of fee, per $1 at the $6 contract count:
  below 0.70  280 entries (36%), 67.1% won at a mean 0.663 ask: -0.8c per $1 [-9.1, +7.6]
              - break-even, NOT a loser; the low win rate is the low price. 4 of 10 days
              negative (09-25 -27, 09-26 -26), 6 positive (09-23 +25).
  0.70 and up 487 entries, 83.6% won: +5.0c per $1 [+0.7, +9.3] - all the profit.
  difference +5.7c [-3.7, +15.2] - not proven.
BUT A 0.70 FLOOR WOULD HAVE COST MONEY UNDER THE LIVE RULES (totals, 10 days):
                            all signals   floor 0.70
  flat $6, no cushion         +111.74      +151.43   (+39.69)
  flat $6, cushion            +130.31      +128.02   (-2.29)
  follow $6/$3, no cushion    +110.98      +119.53   (+8.55)
  follow $6/$3, cushion LIVE  +124.66       +99.81   (-24.86; worse on 7 of 10 days,
                                                      worst day -4.44 vs -1.27)
  "wait until the ask reaches 0.70" instead: +109.53, worse on 6 of 10 days.
WHY: the 5 bps after-loss cushion already removes the cheap entries that lose - it is
the same job done better. What a floor removes under the live rules is mostly cheap
entries taken after a WIN, which paid, and which also reach the 8% target sooner.
NOT A RULE: the 0.70 cut was picked after looking (127's lead), in-sample, 10 days. It
stays a parked hypothesis for the 2,000-signal review, judged on signals from 10-02 only,
and must be tested WITH the cushion, since alone it measures a different system.
DEPLOY INCIDENT, SAID (129): the first suite-then-restart run tested the close-out code,
but the stake-scale patch was applied at 15:30 while that suite ran; the suite passed on
the modules it had already loaded and the restart at 15:45:18 loaded the NEW, untested
first version (the frozen-target defect) - live until 16:30:11. I told the operator
"nothing was restarted"; that was wrong, found from watchdog.log. Effect: none - all three
mirrors were paused for the day (no mirror orders), today's rows had no stored stake so
the failing assignment never ran (no "stake not applied" in the log), the primary traded
at its own $6/$3, close-out was outside its window, no errors. The 16:30 restart runs the
code the final suite (1922 passed) tested. FIX: suite_then_restart_btc.sh fingerprints
src/, scripts/, .env and strategy*.json before the suite and refuses to restart if they
changed; restart_btc_at_open.sh re-checks at the flat moment.

## 131. Could luck have produced this record? (2026-10-01, the operator's question)

OPERATOR: "96 signals a day and coming out in profit, in simulation and live, through
different sessions... nine days, ~800 signals, daily profit. Luck cannot give you such a
result if it is not the system." Asked exactly that way, on the recorded signals:
- 817 signals at the alert won 76.4% where Kalshi's prices implied 73.3%: a system with
  no edge does that ~2 in 100 (z 2.05). The 773 live-rule entries: 77.6% vs 74.1%,
  ~1 in 80 (z 2.26).
- Not one lucky stretch: beat the price on 9 of 10 days (09-28 the one tie, 72.3% vs
  72.4%; 09-22 and 10-01 partial) - ~1 in 90 with no edge - and in all four sessions
  (Asia +54.30, Europe +52.42, US +29.73, US late -0.28 at a flat $6).
- The daily profit is not the target rule's doing: at a flat $6 with no target, 8 of 10
  days were positive (losers -1.27, -5.99).
VERDICT: the edge is ESTABLISHED; the system works under its rules; the money is real
and net of fees. What remains open is the SIZE of the margin, which is thin: 77.6% won
vs a 75.4% break-even with fees, +2.2c per contract [-0.7, +5.1] (z 1.49) - that sets how
hard it can be sized, and is what the 2,000 signals narrow. BTC-specific so far (125).

## 132. The cash-out stays under watch for the 2,000-signal review (2026-10-01)

OPERATOR: "when we do the 2000 signals evaluation, we will have to verify if we still
need to keep the early cash out or remove the cash out... so far it makes us lose about
$4... make sure that we are not leaving money on the table while it adds nothing."
On the pre-registered checklist (127). MEASURE: scripts/cashout_review.py [since] - for
every $ trade the primary sold early, the sale (contracts x price - fee) against holding
to settlement (contracts x won), from allsignal_trades; same entry either way.
SO FAR (09-28..10-01): 114 of 231 filled trades cashed out, ALL 114 winners, 0 losers
saved: -4.74 (-4.2c per cash-out; mean sale 0.990). By day -0.07, -0.93, -1.73, -2.01.
Each loser it ever saves is worth roughly a contract count x 0.99, so the cash-out breaks
even at about 1 reversal per ~100 cash-outs; none in 114 so far. The mirrors copy the
primary's exits at their own size, so the cost scales with them. THE REVIEW: run
`cashout_review.py 2026-10-02` on the 2,000 signals; keep it only if the losers it saves
outweigh the winners it sells. The cash-out is LOCKED (CASH_OUT_CODE) - removing it is the
operator's decision at the review.

## 133. $1,000 on the primary: what the record says, the risk, and the size (2026-10-01)

OPERATOR: "If I funded $1,000 on the primary and traded with this system, what would the
daily profit look like? The risk, and the recommended trade size and risk on that $1,000?"
scripts/thousand_dollar_study.py: recorded BTC signals 09-22..10-01, live rules (5 bps
cushion, 8% target with the follow rule: stake halves at/above it), stake = % of each
midnight opening, compounding; the LIVE cash-out modelled from recorded bids (calibrated:
120 modelled vs 114 live cash-outs, 108 the same trades, 0 losers saved in both).
Recomputed independently to the cent (wf_f18fa8da-ae5); liquidity checked on the archived
book at every signal (820/821): no constraint up to ~$100-500 per signal (best level median
~4,100 contracts); fill risk is price movement (4% of signals > 5c past the ask), not size.
  $1,000 at 2% ($20/$10), with the cash-out: -> $1,582 in 10 days; full days +9.3, +2.9,
  +5.6, -1.2, +9.9, -4.1, +5.8, +8.3 %; worst intraday -11.3% (09-28); max fall -13.6%.
  At 5.6% (today's primary aggression, $56): -> $2,919, but intraday -32% on 09-28, max
  fall -36.8%. Held to result (no cash-out) 2% -> $1,729: the cash-out costs ~15 points at
  this size (-0.61c per contract traded, a quarter of the edge) - review item 132.
RISK beyond the record: net edge +2.34c/contract [-0.53, +5.21]; a 2-SD day at ZERO edge
= -22% at 2%, -34% at 3%, -64% at 5.6%; 5 losses in a row ~68% likely within a month
(-10% at 2%, -28% at 5.6%). Real days were calmer than trade odds predict (p~0.01) - not
assumed to last. Forward expectation at 2% after the cash-out ~+3.6%/day, range about -1%
to +11% (in-sample; the rules were fitted on these days).
RECOMMENDED (advice; sizing is the operator's): 2% of the midnight opening, 1% after the 8%
target (follow rule), recomputed each midnight; at most 3% until the net margin on
post-10-02 signals has a lower bound above ~0.4c; no profit-limiting daily stop (every
-4..-10% stop lowered the result at 2%: +40-56% vs +72.5%) but a -15% circuit breaker
(never fired on the record; caps a broken day); drawdown ladder at midnight: >=20% below
the high -> 1% until back within 20%; >=35% -> stop entries and review. Primary sizing is
LOCKED: implementing it needs the operator's word.

## 134. Forecasting the next window before / at its opening: no edge in the archive (2026-10-01)

OPERATOR: a side model forecasting each 15-minute window's settlement at open-60s (PRE,
nothing from inside the window) and at open (OPEN, strike + opening data); "Kalshi only",
"never use anything related to Binance". Workflow wf_db2be930-eb0 (scratchpad/forecast:
build_dataset.py, evaluate.py, results.txt): 4,103 instrument-windows, 8 instruments,
Kalshi reference series (brti_features) + Kalshi quotes + Kalshi official results; walk-
forward by New York day (7 test days); fixed L2 logistic model, BTC-only and pooled.
- PRE vs 50%: pooled 51.75% [49.4, 54.1], Brier WORSE than 0.5 (skill -0.035); BTC rows
  54.5% [50.6, 58.5] not replicated by the BTC-only model (52.0%). No edge.
- OPEN vs Kalshi's opening price: the price is right 57.7% (BTC 59.2%) - it is seen ~20 s
  after open, so it already carries in-window movement. The model does worse (skill
  -0.049 [-0.075, -0.017]); buying its side when it beats the ask + 2c: 1,823 trades,
  52.3% won vs 53.9% break-even, -1.7c/contract, positive on 1 of 7 days.
- No pre-open quotes exist (0 of 1,006 BTC markets quoted before opening).
- Needed to detect a 2-point edge: ~4,900 independent windows vs 50% (~51 days of BTC;
  ~18 days pooled - instruments agree 70% of the time in the same window, so they are not
  independent); ~5,500 paired windows vs the opening price; and at ~52c asks plus fee a
  trade needs ~4 points over the opening price to make money.
AUDIT (independent): no look-ahead leakage in what was evaluated; every feature
recomputed for all 4,103 rows. BUT the builder read 41 pre-cutover (Binance-era) BTC
observation fields (final_price/side) as a label cross-check - none reached the dataset
(37 dropped, 4 matched Kalshi exactly) - against the operator's rule: any future build
reads BTC observations only from window_open >= 1790135100000 (the Kalshi-only cutover).
Consistent with FINDINGS 36 (no model beats the price, 6,428 markets) and 76 (48 rules at
the money, none). Nothing built or changed live.

## 135. A daily CAP on the primary: stop for the day at ~20% (2026-10-02)

OPERATOR: "look for the max average profit target so that we are not letting profit go
up and down throughout the day and maybe not able to recover on a bad afternoon... once
reached after the first target, primary is done for that day. Test that."
scripts/upper_target_sweep.py (recomputed independently to the cent, wf_f5a49bba-6f8):
live rule on the recorded signals ($6 below 8%, $3 at/above, 5 bps cushion, held to the
result) + no new entry once the day reaches CAP% of 107.33. 9 full days 09-23..10-01:
  no cap (LIVE) +113.77 (795 trades)   8% +72.94    10% +88.37    12% +106.77
  14% +107.32   16% +121.93   18% +131.75 (576)   20% +132.22 (611)   22% +124.64
  25% +118.80   30% = no cap.
WHY: the live days peaked at +17..+27% and gave back $6.87 a day on average (09-28 peaked
+13.40, closed -1.26; 09-30 +25.06 -> +14.55). A cap at 16-22% beats no cap at every
step - a plateau, not a single lucky point - by locking 4-5 of the 9 days near their high;
it never touched the two losing days (09-26 peaked +4.8%, 09-28 +12.5%; only a 12% cap
would have saved 09-28, and it costs more on the good days).
In-sample: best of 11 levels on 9 days; +$18 over 9 days comes from 4 days. Implementing
it changes the LOCKED strategy - the operator's word only.
IMPLEMENTED (operator, 2026-10-02: "I authorize implement the 20% daily stop on the
primary"): DAILY_PROFIT_STOP_RATE=0.20 -> DailyProfitGuard.stop_rate on the primary only.
Once the day's realised P&L (net, broker) reaches 20% of the midnight opening, block_reason
refuses EVERY new entry until 00:00 New York ("BTC 20% daily profit target cap reached;
done until midnight New York" - "profit target" keeps the per-signal miss notice quiet);
latched in profit_days.capped_ms (migrated), so a later loss does not reopen the day; the
live figure counts too, so a settlement since the last refresh cannot let one more entry
through. Exits, cash-outs and recording are untouched; no LOCKED function changed (the cap
lives in the guard, which the order path already consults). Telegram: one "DAILY CAP
REACHED" notice, CAPPED in the accounts table, the cap named in the midnight notice.
The mirrors copy only primary fills, so they also stop when the primary does - they have
normally paused at their own 15% long before. REVIEW (wf_56943a2c-dff): nothing slips past
the cap; two Telegram-only defects FIXED before deploy - the outage pair said "New BTC
entries allowed again" on a capped day, and with the cap switched off mid-day the display
still said CAPPED with a $0.00 cap (now shown only while the cap is in force). Tests:
tests/test_primary_daily_cap.py (11), mutation-checked.

## 136. A give-back stop on top of the 20% cap: worse at every level (2026-10-02)

OPERATOR: "give back no more than $X from the day's high - test that". scratchpad
giveback_stop_sweep.py (recomputed independently to the cent, wf_c4f3b636-e82): the live
rule + the 20% cap, plus: once the day's realised high reached 8% ($8.59 of 107.33), done
for the day if the P&L falls GIVE below that high. 9 full days 09-23..10-01:
  none (live) +132.22 | give 2% +87.28 | 3% +100.09 | 4% +100.09 | 5% +91.54
  6% +99.47 | 8% +113.13 | 10% +105.29 - every level $19-$45 WORSE.
  Armed from the first trade (also a daily loss stop): 4% -31.52 .. 10% +54.57, $78-164 worse.
It does fix 09-28 (-1.26 -> +2.21..+9.94) but cuts the good days that dip and recover
(09-24 +17.91 -> +2.08; 09-25 +22.16 -> +6.70; 09-29 +16.50 -> +9.41), and the money
"given back" mostly RISES (the day stops before the higher high it would have reached).
Same mechanism as the BTC loss stop (126): BTC's intraday dips recover the same day.
NOT ADOPTED. The 20% cap alone stays.

## 137. Can the next LOSS be predicted? No - not beyond the price (2026-10-02)

OPERATOR: "Do we have some pattern in how the signals are coming in daily, like we can
predict when a losing trade is about to kick in (the probability of the next signal to
lose), based on the data, the signal regime and time of day?" Workflow wf_f8dda617-53f,
three independent analyses, Kalshi-only signals (window_open >= 1790135100000): 885
signals, 841 live entries (cushion), 11 NY days; scratchpad/losspat.
- PATTERNS: 61 tests (hour, session, weekday, signal # in day, previous result, run
  length, time since last loss, volatility, distance, momentum, elapsed, day-P&L state,
  cushion entry, ask band) - NONE survives Benjamini-Hochberg at 10% (best p 0.014,
  q 0.38). Closest: high volatility +3.7c, under 20 min since a loss +4.0c, hour 11 +11c,
  hour 20 ET -18c (lost on 6 of 9 days - the only hour bad in both halves), day already
  at/above 8% -1.4c (partly a sequence artifact: chance alone gives most of that gap).
  The 127 lead "hours 08/09/15/20 weak" does NOT replicate: worst-4 hours picked in one
  half are -8c (p 0.40) / -4c (p 0.58) in the other; hour patterns of the halves r=+0.12.
  The 130 "below 0.70" lead also did not repeat in the second half (+0.0 vs -8.1c).
- SEQUENCE: losses do NOT cluster. After a loss the next entry lost 17.4% vs 22.9% priced
  (after a win 24.2% vs 26.7%); runs test: no clustering (p 0.68 all, 0.95 live); longest
  live losing run 3 vs ~4.2 expected by chance; losses per clock hour = chance (p 0.52);
  volatility regime neither causes nor hides clustering. The cushion's real job: the 44
  signals it skipped lost 45.5% vs 33.6% priced; net +$1.95 per contract over 10 days.
- PREDICTION, walk-forward (7 test days, 567 entries, L2 logistic on 27 fixed features):
  WORSE than the price itself (Brier 0.199 vs 0.168; AUC 0.59 vs 0.65); skipping what it
  flagged lost money at every margin (m 0..0.10: -1.82..-21.43 at $6).
- NEEDED: a 2-point loss-rate edge ~3,300 entries (~40 days); inside a 25% skip group
  ~13,300 (~158 days). The price already carries the win probability; the edge is the
  signal itself, not its timing. CANDIDATE to pre-declare for the 2,000 review: hour 20 ET
  (a single hour, q 0.38 - not a rule). Nothing changed live.

## 138. 10-02, first big losing day under the live rules - and "against the 1h trend" tested (2026-10-02 13:00)

BROKER, 13:00 ET: primary -16.07 (-13.4% of 119.67) after a high of +13.86 (target hit
04:45); mirrors all paused at their targets by 05:15 (Wife +6.59, George +3.78, Affoue
+1.62). 47 graded $ trades won 68% at a mean 0.720; UP won 14/28 (priced ~74%), DOWN 18/19.
BRTI rose to ~87,100 by 09-10h then slid ~200 bps to 85,127; seven $6 UP losses 09:00-12:15
took the day from +8.57 to -16.07 (the follow rule put the stake back to $6 below 8%).
A fall of ~$30 = ~5 stakes from the high: inside the risk range of 133 (5-loss runs ~monthly).
HYPOTHESIS FROM TODAY, TESTED ON THE PREVIOUS DAYS ONLY (Kalshi BRTI 60-min move before the
alert): with the 1h trend +2.26c [-3.2, +7.7] n=211; AGAINST +1.44c [-5.5, +8.4] n=146;
flat hour +1.60c n=430 - NO difference on the record. Today: against the trend 43% won,
-28.9c (n=14). One day; pre-declare "against the 1h trend" with hour 20 ET for the 2,000
review. The give-back stop (136) and pause-at-target would have helped today; both stay as
they are until the review weighs days like this one with the rest. Nothing changed live.

## 139. Why the signal keeps going against the trend, and what would have helped (2026-10-02)

OPERATOR: "Why do these keep choosing to go against the trend - all 4 losses. Check what
could have helped the signal quality." MECHANISM (src/btc15_signal/model.py:18): ~4 min into
each window, side = the side of the strike BRTI is on; the score uses only normalized
distance and 5-min momentum - no longer trend. In a slide, an early intra-window bounce puts
the price above the strike with fresh +5..+17 bps momentum, so it scores as a strong UP;
11:45, 12:00, 12:15, 12:45 were exactly that (primary -21.40 at 13:04).
TEST (wf_c60a3eb6-f12, scratchpad/trendfix; 16 pre-registered rules, live rules incl.
cushion, follow, 20% cap; recomputed independently to the cent; no look-ahead):
  previous 9 days: UP +1.6c, DOWN +1.5c per contract - neither side worse; against-trend
  signals within ~1 SE of the rest under every definition. TODAY: UP won 14/29 vs 21.8
  expected (z -3.4) - nothing like it in the record (worst side-day -1.0 SD).
  Rules (change vs live, previous 9 days / today): skip vs 60m>10bps +1.64 / +33.25;
  vs 15m>10bps +3.40 / +5.33; vs 60m>20bps -36.94 / +25.88; vs 30m>10bps -20.80 / +30.21;
  half stake -14.77 / +5.74; confirm 10/15/20 bps -35.61/-33.34/-25.94 / +5..+17;
  day trend -34.86 and -5.09 / -32.60 and -21.49; side cooldown -7.35 / +1.96.
  The best previous-days result (+3.40) is what chance gives (69% of shifted-flag runs).
VERDICT: nothing would have helped without hurting the record or fitting today; what
protected money today was money management (the mirrors' pause at target), not signal
quality. PRE-DECLARED for the 2,000-signal review (judge signals from 10-03 ONLY): primary
hypothesis = against the 60-min trend by > 10 bps (trendfix.py definition), test = flagged
minus rest net per contract at the alert price, pass = <= -3c and >= 2 SE below zero AND the
skip rule does not lose vs live day by day; secondary (report only) = 15-min > 10 bps;
expected underpowered at 2,000 (~1.2 SE) - carry to ~3,000 new signals if inconclusive.
Also re-weigh pause-at-target and the give-back stop with days like 10-02 included.
Nothing changed live.

## 140. The trend skip applied ONLY after a loss (2026-10-02)

OPERATOR: "What [about] apply that only after a loss" (the cushion's moment). The audited
trendfix.py engine + one condition: when the day's last taken entry lost, skip a signal
that is against the trend (a skipped signal leaves the last result a loss, as the cushion
does). Recomputed independently (wf_3740bc9a-dc6) - previous days to the cent.
  change vs live            previous 9 days (better/worse days, signals skipped)   today
  after loss, 15m > 10 bps  +0.22  (3/4, 17 skipped: 10 won = 59% vs ~71% priced)  +11.67
  after loss, 15m > 20 bps  -1.29  (2/2, 7)                                         +6.11
  after loss, 30m > 10 bps  -1.18  (2/5, 26)                                        +11.67
  after loss, 30m > 20 bps  +0.69  (3/1, 10)                                        +6.11
  after loss, 60m > 10 bps  -10.62 (3/6, 69)                                        +23.29
  after loss, 60m > 20 bps  -22.99 (3/5, 50)                                        +22.62
After a loss on the previous days: against the 60m trend -0.8c (n 52) vs +0.2c the rest -
no difference; against the 15m trend -13.7c (n 17, SE 12c) - in the right direction, far
from proven. READ: applied only after a loss the short-trend versions are ~free on the
record (they touch ~2 signals a day) and help on a day like 10-02; the 60m version costs.
Evidence for a benefit is weak (17 signals, found on the day it is tested on). The cushion
code is LOCKED (CUSHION_CODE): adopting needs the operator's word. Otherwise pre-declared
for the 2,000 review with 139.

## 141. The 15-min after-loss rule in shadow on BTC: fewer repeat losses (2026-10-02 13:50)

OPERATOR: "After each loss, did the rule-based system running in the shadow have the same
consecutive loss or did it do better? ... BTC running in the shadow with the rules."
Same BTC signals 09-23..10-02 (13:15 window), the audited trendfix engine; LIVE vs SHADOW =
live + "after a loss, skip a signal against the 15-min BRTI trend by > 10 bps":
                         LIVE        SHADOW (15-min rule)
  net                    +104.49     +116.38
  next trade after a loss lost   30/146 = 21%   23/145 = 16%
  losing streaks 1 / 2 / 3+      96 / 16 / 7    106 / 15 / 4  (longest 3 both)
The rule acted 18 times: avoided 8 losses (+37.28: 09-24 x2, 09-28 01:45/03:15/03:30,
09-29, 10-02 10:15/12:15), missed 10 wins (-13.93, $0.45-3.01 each). Asymmetry: a skipped
loser saves the stake (~$5-6), a skipped winner gives up ~1/3 of it, so it pays above ~1
loser caught in 3; it caught 8/18. Knock-on path effects (09-25) leave +11.89 net.
Small sample, idea suggested by 10-02. OPERATOR: "keep our 15 minutes in mind for
implementation" - the PENDING candidate; implementation (in the LOCKED cushion code) on
the operator's word, with tests + review before live.

## 142. The 15-min skip after TWO losses instead of one (2026-10-02 14:01)

OPERATOR: "test the 15 minutes to only trigger after two losses, not at the first loss."
Same engine; the skip arms only when the day's last TWO taken entries both lost (a skipped
signal leaves the streak as it is). 09-23..10-02 (live now +107.41 with today's later
windows):
                         net (vs live)    acted  avoided / missed         days +/-  repeat-loss  3+ streaks
  live                   +107.41           -      -                       -         20%          7
  after 1 loss           +116.38 (+8.97)   19     8 (+37.28) / 11 (-16.85) 4/4      16%          4
  after 2 losses         +121.64 (+14.23)   4     2 (+11.05) / 2 (-2.48)   3/1      19%          5
After-2 acted: 09-28 03:30 UP (avoided -5.48), 09-29 08:30 UP (missed +1.14), 10-01 14:15
DOWN (missed +1.34), 10-02 12:15 UP (avoided -5.56). It breaks the third loss in a row and
avoids after-1's cost on normal days (09-25 -6.38) but rests on 4 events; after-1 cuts more
repeat losses on trending days but interferes ~2x a day. Neither proven better than the
other. Both PENDING the operator's choice (memory btc15-pending-15min-rule).
AFTER THREE LOSSES (operator: "test after 3 losses"): never acts - +107.41, identical to
live. Three-in-a-row happened 7 times in 10 days and the NEXT signal won every time (7/7:
09-24 14:15, 09-28 03:45 / 20:45 / 23:15, 09-29 15:45, 10-02 06:30 / 12:30), none against
the 15-min trend; no 4-loss run exists. The useful moment is after the 2nd loss.

## 143. After two losses, never against the 15-min trend - chosen, mirrors checked, built (2026-10-02)

OPERATOR: "the 15 minutes after 2 losses is the one I want live, nothing else changes to the
live rule. But before that test that with the mirrors hitting their targets."
MIRRORS (copy the primary's fills at their own stake, pause at their own target; openings
of 10-02; 09-23..10-02): targets hit 9/10 with and without the rule; Wife +51.62 -> +52.26,
Uncle George +33.07 -> +32.85, Affoue +16.48 -> +15.84 (only 09-28 differs: targets hit at
16:30 instead of 18:00). Primary +107.41 -> +121.64. The rule leaves the mirrors as they were.
BUILT: main.allsignal_loss_streak / brti_trend_bps / allsignal_trend_skip, called at the top
of allsignal_on_alert - after the day's last TWO taken $ trades both lost (unknown result =
loss), a BTC signal against the 15-min Kalshi BRTI move (brti_features; valid at t only if
ts_ms and received_ms <= t, <= 60 s old, stale=0) by > 10 bps is recorded 'skipped' (the
miss notice says why) and no order is sent; the streak stands until a trade is taken.
ALLSIGNAL_TREND_SKIP_AFTER_LOSSES=2 (_MINUTES 15, _BPS 10); default 0 = off; BTC only;
errors let the signal through. CUSHION_CODE re-pinned 07d0c8be54297726 with the three new
functions inside the lock. Tests: tests/test_allsignal_trend_skip.py (11), mutation-checked
(4 of 4 caught).

## 144. The primary under the mirrors' rule (pause at target) - 10 days (2026-10-02 14:19)

OPERATOR: "If primary followed the target rule like the mirrors, what would its result be?"
trendfix engine, 09-23..10-02 (today through the 14:00 window), $6, cushion; C 107.33 /
today 119.67:
                                total    trades  losing days  worst    today
  now: $6/$3, 20% stop          +109.77   668     3            -22.45   -22.45
  now + after-2 rule (going live) +124.00 664     2            -16.88   -16.88
  PAUSE at 8% (mirror-style)    +83.32    251     1            -1.27    +10.38
  PAUSE at 8% + after-2         +86.39    245     1            -1.27    +10.38
  PAUSE at 15% (mirror rate)    +89.14    458     3            -22.45   -22.45
Pause at 8% is the steadiest (+8.6..+11.7 on 9 of 10 days, one losing day -1.27, today
+10.38) but makes $26-38 less: it gives up the good days' upside (09-23 +21.85 -> +10.14).
The current rule + after-2 makes the most and carries the bad days (today -16.88). In-sample.
Nothing changed.
REVIEW (wf_70bd5fd9-d9d, before deploy): the live functions reproduce the study exactly on
the live archives (same skips: 09-29 08:30, 10-01 14:15, 10-02 12:15; 09-28 03:30 predates
the $ book; brti trend equal on all 897 signals). Two defects FIXED: (1) an UNKNOWN result
counted as a loss and the skip is final - two WINNERS still unsettled at 09-29 21:34 would
have armed it (the signal was DOWN, so nothing happened); now only KNOWN losses arm it (the
cushion still waits on unknown, as before); (2) the skip's DB write was unguarded on the
alert path - now guarded. Refuted: cash-out P&L in the streak. CUSHION_CODE re-pinned
e8307b4332e4a14c. 14 tests, mutation-checked. Full suite + fingerprint-guarded restart
started 2026-10-02 ~14:40.

## 145. Every instrument's shadow record side by side (2026-10-02 14:47)

OPERATOR: "how has gold been doing" / "check all the others". Kalshi-only signals
(window_open >= 1790135100000), every primary signal at its alert price (no cushion - it is
BTC-only), net of fee; "rules" = the primary's $6/$3 at 8% of 107.33, stop at 20%. All eight
instances recording (last observation 4-10 s old).
  inst   signals days  won    break-even  net/contract [95%]       flat $6   rules    losing days
  BTC    899     11    75.6%  74.6%       +1.03c [-1.7, +3.8]      +76.23    +93.62   3/11
  ETH    759     9     75.1%  75.9%       -0.78c [-3.8, +2.3]      -26.68    -1.31    4/9
  SOL    668     8     75.4%  75.5%       -0.03c [-3.2, +3.1]      -4.40     -38.50   4/8
  XRP    588     7     71.1%  75.1%       -3.99c [-7.6, -0.4]      -187.16   -182.75  4/7
  NEAR   576     7     72.6%  76.2%       -3.64c [-7.2, -0.1]      -160.98   -152.06  6/7
  BNB    100     2     74.0%  73.4%       +0.60c [-7.9, +9.1]      +8.50     -10.21   1/2
  GOLD   519     8     71.9%  73.7%       -1.80c [-5.6, +2.0]      -81.50    -76.77   6/8
  SILVER 469     7     72.7%  74.7%       -1.98c [-6.0, +2.0]      -62.22    -40.68   2/7
Only BTC wins more often than its price (and with its cushion, 77.6% vs 75.5%). ETH and SOL
are at break-even; XRP and NEAR lose beyond chance (their intervals sit below zero); GOLD
and SILVER lose but within noise; BNB is too new. None qualifies for live; all stay shadow.
XRP/NEAR: the configs fitted on Binance-era data (75) do not hold on Kalshi - refit or drop.
LIVE 2026-10-02 15:00:11 (suite passed on code ce446088b6b29941; BTC 21020 -> 6704, started 15:00:23 clean; settings loaded: after 2 losses, 15 min, 10 bps).

## 146. Losing streaks vs volatility; the low-volatility cushion lockout (2026-10-03 08:23)

OPERATOR: "were the consecutive three to five losses happening because of high volatility
days?" scripts/loss_streak_volatility.py on the ACTUAL live $ trades 09-28..10-03 08:00
(352 trades, 86 losses; Kalshi BRTI): 4 losing streaks of 3+ (15 losses):
  09-28 20:00-20:30 3 (DDU)    15m vol pct 48, |60m move| pct 22 (-6 bp)   typical
  09-28 22:15-23:00 4 (UDUU)   pct 69, move pct 26 (+9 bp)                 moderate
  09-29 15:00-15:30 3 (UUU)    pct 24, move pct 57 (+23 bp)                LOW vol
  10-02 11:45-13:00 5 (UUUUU)  pct 81, move pct 69 (-34 bp)                high vol + downtrend
Streak losses 0.85 bp 15-min vol vs 0.77 for wins and other losses. Loss rate by vol third:
low 26.5% (priced 27.5%), middle 20.5% (26.3%), high 26.5% (25.5%) - no volatility effect.
The widest-range day (09-30, 313 bp) had no 3+ streak. VERDICT: volatility does not explain
the streaks; only the worst (10-02) was high-vol, and it was a TREND (bounce in a slide) -
what the after-2-losses rule (143, live 10-02 15:00) addresses.
10-03, the opposite case: very LOW volatility (0.22 bp avg 15-min vol, 39 bp range by
08:00). After the 04:30 loss the cushion skipped every signal to 08:13+ (price never 5 bps
clear); 19 cushion skips today would have won 13 (68%) yet netted -12.87 at $25 - the skips
saved money overall, but kept the primary out from 04:45 (incl. 7 would-be winners 06:45-08:00).
SIZING (operator, .env 2026-10-02 23:41, restart 23:58, same code): primary funded to
~$996, ALLSIGNAL_STAKE=25, ALLSIGNAL_AFTER_TARGET_STAKE=10 (2.5% / 1%); Affoue ~$120.71.
SATURDAYS (operator: "is this our second or third Saturday?"): 10-03 is the SECOND Saturday
of Kalshi-only signals (record starts Tue 09-22 23:45) and the FIRST of live $ trading
(live from Mon 09-28 14:15). Both are the calmest days on record and both below break-even:
09-26 97 signals won 71% at 0.72, BTC range 82 bp, 12-s vol 0.36 bp (live-rule replay
-1.27 at $6, cushion skipped 28 of 96); 10-03 to 08:15 36 signals won 69% at 0.71, range
46 bp, vol 0.25 bp (weekday ranges 189-382 bp). Sunday 09-27 was strong (+22.79). Two
days: a lead only - pre-declare "quiet day / Saturday" for the 2,000 review.

## 147. Quiet days: take every signal instead of the cushion's skips? No (2026-10-03 19:30)

OPERATOR: "evaluate all the Saturday signals and the skips - if taking all on quiet days
would have helped, make a check for quiet day and take all only on quiet days, verify."
scratchpad/trendfix/quiet_take_all.py (recomputed independently to the cent,
wf_1d78b1e1-094): the live setup since 10-02 23:41 ($25/$10 on 995.93, cushion, 20% stop,
after-2 trend skip), 11 days 09-23..10-03, quiet judged LIVE at each alert (Kalshi BRTI);
when quiet, take the signal at its alert price (no cushion, no trend skip):
  live                          +666.56   Sat 09-26 -0.35   Sat 10-03 +6.54
  quiet = 60-min range <= 15 bp  -85.92   | <= 20 bp -41.56 | <= 30 bp +3.90 (days 6/5 - noise)
  quiet = day range so far <= 50 bp -11.65 | <= 80 bp -46.09
  Saturdays: take all           -47.49   (09-26 -56.21, 10-03 +14.91)
  always take all               -213.50
The cushion's skips: 09-26 28 skipped, 17 won (61% vs 72% for the day) - skipping them was
worth +55.86; 10-03 27 skipped, 18 won (67% vs 74%) - taking them would have added +8.37.
Quiet signals win as often as the rest (215: 75.8% at 0.718 vs 798: 75.6% at 0.735).
VERDICT: no - the cushion helps on quiet days too; no quiet-day exception. Nothing changed.

## 148. Telegram: the Kalshi target on every $ signal message (2026-10-03)

OPERATOR: "Can we have the signal in telegram show the Kalshi target price for clarity."
Display only (no LOCKED function touched; locks pass). messages.target_lines + optional
target/ref/final on the entry, result and skipped/not-filled messages; main._window_prices
reads what the service already records: the alert's observation (target = the window's
strike, equal to Kalshi's official strike; btc = the Kalshi BRTI reference at the signal)
and the synced settlement (strike, expiration_value). Entry/skip: "📍 Kalshi target
$84,787.45 · UP wins at or above it" + "💲 BTC at the signal $84,810.91 (+2.8 bps above)".
Result: "📍 Kalshi target $84,787.45 → settled $84,862.28 (+8.8 bps above)". Unknown
values leave the line out; never raises. Tests: tests/test_signal_target_line.py (6).
REVIEW (wf_4f5bb4c7-9fe): nothing blocking; numbers verified on live windows (target =
settlements.strike in every window; Kalshi YES at or above the strike). Two fixes applied
before deploy: (1) the settlement sync lands ~60-70 s AFTER the result message (24 of 25
results would have had no settled value) - the result now falls back to the NEXT window's
strike, which Kalshi opens at this window's settled value (matched 8/8); the official
value wins when present; (2) "BTC at the signal" now names the instrument. 8 tests.
LIVE 2026-10-04 00:30:09 (suite 1955 passed on code 1b85934543f7775f; BTC 4848 -> 19744, started 00:30:24 clean).

## 149. Avoid weekends? No - weekend signals do as well as weekday ones (2026-10-04 13:45)

OPERATOR: "Should we avoid weekend?" Kalshi-only BTC signals at the alert price, net of fee:
  weekday 744 signals, won 75.4% at 0.730, +1.03c per contract [-2.0, +4.1]
  weekend 343 signals, won 75.5% at 0.733, +0.90c per contract [-3.6, +5.4]
Per weekend day: Sat 09-26 -1.36c, Sun 09-27 +5.64c (the best day per contract on record),
Sat 10-03 -0.75c, Sun 10-04 -0.54c (to ~13:30). Weak weekdays are just as common (Mon 09-28
-2.92c, Fri 10-02 -1.39c). Skipping weekends would have cut 09-27 (+109.35 at $25 under the
live rules). Four weekend days: no basis to avoid them; weekend vs weekday stays a report
line for the 2,000-signal review. Nothing changed.

## 150. The rule-based (main) strategy in shadow vs the all-signal (2026-10-05 09:35)

OPERATOR: "How is the rule-based system doing in the shadow?" NOTE: trade_proposals
'primary' status 'pending' (1,073 since 09-23) are NOT the gated rule - they are created for
every signal, same time (285 s in) and side as the all-signal alert, at 0.65-0.96 (434 below
the 0.70 floor). The gated rule is observations.rule_match = 1; its trade = the first such
poll per window (at its our_ask), graded by the window's settlement for that side:
  RULE-BASED  440 trades / 14 days (~31/day), won 82.0% at 0.805, +0.45c per contract
              [-3.1, +4.0]; $25 each +70.86; 6 of 14 days losing (10-04 -175, 09-26 -111;
              09-27 +125, 10-03 +104).
  ALL-SIGNAL  1,154 signals 09-23..10-05, won 75.3% at 0.732, +0.75c [-1.7, +3.2]; $25 each
              +295.80 (the live rules - cushion, target - did better still).
  BRAIN-approved alerts: 1,149 of the 1,154 - effectively the all-signal (+0.68c).
The rule wins more often but pays ~7c more per contract and trades a third as often:
less per contract and far less in total. The all-signal stays the right live strategy.

## 151. What Sat 10-03, Sun 10-04 and Mon 10-05 (to 09:40) teach (2026-10-05)

KALSHI (net): primary -4.29 / -26.92 / -41.44 (to 09:40) = -72.65 (-7.3% of ~$996); Wife
+0.24 / -4.42 (-10.4%) / -5.50 (-14.5%), opening 42.35 -> 37.93; George and Affoue small.
1. THE SIGNAL HAD NO EDGE these days: won vs break-even -0.7 / +0.4 / -2.0 points (73% at
   0.723; 75% at 0.733; 76% at 0.771). With ~zero edge the costs make every day negative.
2. THE COSTS NOW MATTER IN DOLLARS at $25: entries paid +2.4c / +1.5c / -1.0c vs the signal
   price (cushion waits buy dearer); cash-out since 10-03: 93 of 165 trades sold early, all
   winners, -31.63 (-34c per cash-out), no loser saved.
3. THE CUSHION was mixed: skipping saved 92.93 (Sat, skipped won 61%), cost 37.19 (Sun,
   skipped won 76%), saved 13.59 (Mon) - net ~+69 over the three days.
4. UNFILLED SIGNALS ALL WON: 3 + 4 + 3 = 10 of 10 - the ask ran past the cap because the
   market moved the signal's way. A missed fill is systematically a winner. LEAD: test a
   wider entry cap / a later retry at the moving price on the record before changing it.
5. The after-2 trend rule never acted (no two-loss run against the trend).
6. Wife's $3 is ~8% of her balance per signal: -10% and -14.5% days. Her stake is the
   operator's floor; the 2% scale would put her at $1.
Three days (~230 signals) of ~zero edge sit inside the noise of a +0.75c edge; nothing
changed.

## 152. Cash-out OFF; chasing the unfilled signals (2026-10-05)

OPERATOR: "Disable cash out for now and also test [the] new finding - it's best to make a
few profit than to let it go completely."
CASH-OUT: CASH_OUT_ENABLED=false in the live .env (its own switch; no code change, the
LOCKED cash-out returns at its first line); every $ trade held to settlement. Since 10-03
the cash-out had sold 93 winners early for -31.63 and saved no loser. tests/conftest.py
sets CASH_OUT_ENABLED=true for the suite (as for the recoveries) so its mechanics stay
tested; tests/test_allsignal.py pins the OFF behaviour (no sale, no mirror exit).
UNFILLED (scratchpad/unfilled_chase.py): 30 live unfilled $ signals 09-28..10-05, 25 won
(83%) - the earlier "10 of 10" was only 10-03..10-05. The recorded quotes LAG the book: at
the alert they still show the signal price although the order failed at signal + 5c, so
they cannot price a chase. At the MOVED price (first recorded ask above the cap after the
failed order; known for 29): buy up to 0.85 -> 24 fills, -2.74; up to 0.90 -> 27, +6.46;
up to 0.93+ -> 29 fills (24 won, 5 lost) at 0.801 avg, +11.13 at $25 over 8 days. A win at
~0.80 pays ~$5, a loss costs ~$25: break-even ~83% won, and they won 83%. VERDICT: about
break-even - not worth changing the order path now. Nothing changed for unfilled signals.

## 153. Early exits on the Kalshi lifecycle: keep them OFF (2026-10-05)

OPERATOR: "the trade exit we disabled ... because we were exiting trades that eventually
turned out to win - I believe we have enough data now to test the exit using the trades'
life cycle." Workflow wf_5be1ad8a-9c4 (scratchpad/exitstudy): 1,069 live-rule entries
09-22..10-05 (Kalshi only), hold to settlement +425.71 at $25; 15 pre-registered rules,
selling all at the first qualifying poll with >= 60 s left:
- BID STOPS (bid <= 0.15..0.55): lose in every version, both halves (-177 .. -646); the
  trades they sell won 1-2c MORE often than the bid implied - the operator's "exits that
  turned out to win".
- CROSS / DEEP-CROSS STOPS: looked slightly positive at the recorded quote (best C 0 bps
  <= 4 min +166.68, t 1.25, family-wise p 0.23-0.47) - but THE RECORDED QUOTE LAGS THE
  ORDER BOOK 20-30 s (observations come from the /markets list endpoint; book recorder
  snapshots match the quote 2-3 snapshots later; book top changes on 87% of snapshots, the
  quote on 28%). BRTI is fresh, so a cross stop fires before the quote prices the cross.
  At the real book price ALL 15 rules lose (C 0 <= 4 min -234.61 / -112.42; D -10.27 / -20.30).
- 10% of losses stay on the held side until the last minute; no rule can catch them.
VERDICT: the market prices an adverse move correctly (FINDINGS 40, now on Kalshi's own
book); exits sell at or below fair value. KEEP OFF. Any future exit or entry-price work
must price from the order book (/orderbook), not the lagged /markets quote - this lag also
explains why recorded asks looked "fillable" for orders that missed (152) and the open
"book vs quote" item of FINDINGS 7.

## 154. What the losses since Saturday share: cheap prices - but a floor still costs (2026-10-05)

OPERATOR: "What do these new losses since Saturday have in common when it comes to signal
quality and price?" Live $ trades, at the alert (Kalshi BRTI for trends):
                      losses since Sat  wins since Sat  losses before  wins before
  price paid          0.727             0.781           0.697          0.746
  bps from target     5.2               6.4             6.3            8.4
  5m volatility       0.71              0.73            1.13           1.08
  15m / 60m trend to side +4.9/+4.2     +6.3/+6.5       +4.1/-0.1      +7.7/+7.2
  bought under 70c    46%               21%             45%            32%
  against 15m trend   8%                7%              12%            4%
  UP share            54%               55%             63%            48%
Win rate under 70c: before Sat 82/119 (69%, avg 0.641 - above break-even); since Sat 27/45
(60%, avg 0.651 - below ~66% break-even). 0.70-0.80: 72% -> 81%; 0.80+: 89% -> 84%. Side,
momentum, distance/volatility, the 15-min trend: no difference. The regime is quieter
since Saturday (5m vol 0.71 vs ~1.1) for wins and losses alike.
A FLOOR under the CURRENT live rules (scratchpad/trendfix/floor_now.py, 13 days, $25/$10):
live +617.12; floor 0.65 -112.57; 0.68 -490.82; 0.70 -381.07 (since Sat +31.23, before
-412.31; worse on 9 days, better on 4). The cheap entries carried the earlier days; the
floor costs far more than it saves (also 130). Not adopted.

## 155. The chase: a missed $ order is bought at the moved price (2026-10-05)

OPERATOR: "You miss the point - whether we take small wins or not it changes nothing, it's
better to take it than just letting it go." Decision logged with the evidence beside it
(152: 30 live misses, 25 won; at the moved price +11.13 over 8 days - break-even-ish, positive).
BUILT in main.allsignal_retry_poll (RETRY_CODE re-pinned ea7e1e26df10b098 on the operator's
word): when an order went unfilled and the ask on its side is now ABOVE its cap but <= 93c
(ALLSIGNAL_CHASE_MAX=0.93), from 10 s after the last attempt (allsignal_chase_after_s), at
most 3 retries (allsignal_chase_attempts), while >= 2 min left and the price is on the
signal's side (after a loss: >= 5 bps): new cap = min(93c, ask + 5c), the same dollar stake
at the moved price, the row's stake/count/limit updated. A price back under the old cap keeps
the 60 s same-cap retry. Default 0 = off; tests/conftest.py keeps it off for the suite and
tests/test_allsignal_chase.py (8) switches it on; mutation-checked (the ceiling is guarded
twice). Live after the full suite + review + fingerprint-guarded restart.

## 156. $10 base, $25 twice after a loss (2026-10-05 10:51)

OPERATOR: "base at $10 and after a loss go $25 twice then back to $10 - test that."
scratchpad/trendfix/boost_after_loss.py (audited engine; cushion, after-2 trend skip, 20%
stop, held to the result; opening 995.93; 13 days 09-23..10-05):
                                   total     vs live  worst day  lowest in a day  avg stake
  LIVE ($25 -> $10 at 8%)          +617.91   -        -44.36     -142.81          $20.74
  BOOST $10, $25 x2 after a loss   +530.92   -87.00   -29.40     -140.26          $16.23
  BOOST only below the 8% target   +621.67   +3.76    -29.40     -140.26          $15.50
  BOOST x1 after a loss            +423.18   -194.74  -15.95     -75.82           $13.38
  flat $10 / flat $25              +170.44 / +491.61
BOOST vs live: better on 6 days, worse on 7; the last three (weak) days +115.31 better
(10-03 +81.23 vs +32.54, 10-04 -7.74 vs -30.28, 10-05 -0.28 vs -44.36), the ten before
-202.31. Why it can work: after a loss the cushioned entries win more often (137: lost 17%
vs 23% priced), so the $25 lands on the better trades. "Only below 8%" keeps live's money
with ~25% less staked per trade and a smaller worst day. In-sample; operator's decision.
REVIEW (wf_9227437b-524) - the first chase deploy was STOPPED before its restart: a retry
sent the order at the signal's OLD ask, and every mirror sizes its copy by the order's entry
price - on a chased order the mirrors would have over-spent their stakes by up to ~50%
(Wife's $3 -> 5 contracts at ~$4.30). FIXED: a retry is sized and sent at the CURRENT price
(the record and messages keep the signal price); and a chase-due poll is urgent from the
miss (it read the price after the poll's chores, 1-2 s late). RETRY_CODE re-pinned
8ad21fa8451f4823; tests 10; the after-target retry pin updated to the new line (still sized
at the stake now). Redeployed 10:59 (suite + fingerprint-guarded restart).
TODAY vs THE WEEKEND (operator: "is today a quiet day like Saturday and Sunday?"), the first
11 hours of each day: Mon 10-05 range 146 bp, 12-s vol 1.05, net -52 bp - a normal weekday
(Mon 09-28: 143 bp / 1.13). Sat 10-03 50 bp / 0.27, Sun 10-04 71 bp / 0.34, Sat 09-26
51 / 0.34, Sun 09-27 95 / 0.55: every weekend so far was 3-4x quieter than any weekday.

## 157. A Binance call was still running: the microstructure recorder (2026-10-05)

While checking which "buying/selling pressure" data exists, found that the separate
scheduled task BTC15Recorder (pythonw -m btc15_signal.recorder --every 10, elevated, boot +
logon triggers, running since the 09-25 reboot) still calls Binance's public API every 10 s
(bookTicker, depth, aggTrades) - an ESTABLISHED connection to data-api.binance.vision
(52.196.191.78) from PID 7384 at 11:0x - against the operator's rule ("Never use anything
related to Binance", "Kalshi only"). The trading service never traded on it, but
main.book_metrics copied its depth_*_qty / trade_count / buy_volume / sell_volume / vwap into
every observation. The model's two pressure terms (bid_imbalance, taker_imbalance) were
already zeroed under Kalshi-only. FIX (applied after the chase deploy, to keep that deploy's
fingerprint): recorder.py writes NULL for every Binance column and makes no Binance request;
the Kalshi quote and order book are recorded as before; tests/test_recorder_kalshi_only.py.

## 158. Kalshi buying/selling pressure does not validate the direction (2026-10-05)

OPERATOR: "Can we add a read for buying and selling pressure to validate the direction?"
Workflow wf_97e2bf4f-d7a (scratchpad/pressure; KALSHI ONLY - book_yes/book_no levels,
volume; no Binance column read, audited): 1,172 signals, book at the alert median 4.8 s old.
  P1 book lean to the side within 5c: with - against +1.2c (p 0.69)
  P2 whole-book depth to the side: +2.7c (p 0.14)
  P3 the side's best bid over 60 s / 120 s: +2.4c (p 0.28) / +5.2c (p 0.21; +2.0c at the
     fresh book price, p 0.63);  P4 (mid, 60 s) = P3_60;  P5 volume: activity only, nothing.
Filters: skip when the book leans hard against the side (P1 < -0.4): those signals WON MORE
(91.7% at 0.814; -130.93 under the live rules) - the wrong way, fragile to the cut-off;
skip when the side's bid fell >2c in a minute: +6.36 under the live rules, a lagging-quote
artifact (at the fresh book price the skipped make +1.0c like the rest; halves -45.80 /
+52.16). With the price held fixed, no pressure measure adds to the win rate (all p >= 0.26).
0 of 19 tests survive BH (best q 0.24); FINDINGS 68 and 137 again. NOT ADDED. The price
already carries the pressure. (If ever used live, key on book_ms, not captured_ms.)
DONE 2026-10-05 11:32: recorder.py patched (no Binance request, the BINANCE URL constant
removed, Binance columns written NULL), tests/test_recorder_kalshi_only.py; task
BTC15Recorder restarted (old PID 7384 gone; new 11:32:06). Verified: 0 established
connections to Binance hosts; new book_snapshots every 10 s with the Kalshi quote and book,
every Binance field NULL. THE CHASE went live at 11:30:08 (suite 1966; BTC 11308 -> 19556;
ALLSIGNAL_CHASE_MAX=0.93, after 10 s, 3 tries).

## 159. No rule-based gate helps the all-signal - as a filter or a size booster (2026-10-05)

OPERATOR: "can any of the rules in the rule-based system help the all-signal at all?"
Workflow wf_7141325c-5da (scratchpad/gates; tester + skeptic who rebuilt the engine to the
cent): live rules ($25/$10, cushion, after-2 trend skip, 20% stop, held), 13 days, LIVE
+637.26 (1,172 signals). The rule that RUNS under Kalshi-only is strategy_kalshi.json
(kalshi_signal.evaluate): price 0.70-0.93, BRTI distance 10x/15x, move still working, level
held >= 120 s, level tested >= 2, BRTI stability, fresh reference, 360-660 s window.
  filter (skip if it fails) / boost (fails -> $10) vs live:
  price band -344 / -260; distance floor -638 / -434; move working -176 / -79; level held
  -279 / -181; level tested -384 / -158; stability -59 / -55; the whole deployed rule -687 /
  -535; retired strategy.json gates (1.5x distance -174/-98, raw prob -220/-133, "7 min"
  -393/-337); momentum aligned -327/-254 and the 2-4x band -984/-575 (both Binance-era
  "confirmations" now point the WRONG way: momentum-against signals +12.4c, aligned +0.3c).
  None beats live in total or in both halves; nothing survives correction. A real booster
  (2x stake when passing) "beats" live only by staking more (doubling everything +535); vs
  200 random gates with the same daily pass counts, no gate beats chance (best: price band,
  21.5% of random gates did as well) and the whole deployed rule did worse than 97.5%.
WATCH (not a rule): since 10-02 the deployed rule as a filter did better on all 4 days
(+127.37) after 9 worse days (-808.73); 29 passing signals - judge at the 2,000 review.
The archive's rule_match on 09-23 recorded the retired strategy.json verdict for 64 alerts.

## 160. The size increase after a loss - variants on all days (2026-10-05 12:29)

OPERATOR: "test that size increase on all the days with variant." scratchpad/trendfix/
boost_variants.py (audited engine; cushion, after-2 trend skip, 20% stop, held; 995.93;
13 days 09-23..10-05 12:15; halves split at 09-29; "boost" = the next N TAKEN trades after a
loss at the high stake, a loss inside restarts it):
                                       total    vs live  1st/2nd half   Sat-Mon  worst  lowest  avg $
  LIVE $25, $10 once at 8%             +642.58  -        -              -        -30.28 -142.81 20.77
  A $10, $25 x2                        +546.12  -96.45   -55 / -41      +105.87  -29.40 -140.26 16.21
  B as A, only below 8%                +636.88  -5.69    -3 / -3        +105.87  -29.40 -140.26 15.48
  C $10, $25 x1                        +438.38  -204.19  -146 / -58     +92.62   -15.95  -75.82 13.38
  D $10, $25 x3                        +628.31  -14.26   -30 / +16      +109.76   -6.89 -126.06 18.36
  E $15, $25 x2                        +515.33  -127.25  -64 / -63      +70.65   -19.89 -144.53 19.14
  F $10, $30 x2                        +671.43  +28.85   +18 / +11      +138.85  -37.30 -166.79 18.28
  G $15, $30 x2                        +640.63  -1.94    +9 / -11       +103.63  -27.79 -171.05 21.21
  H live + $25 x2 after a loss past 8% +622.14  -20.44   +6 / -26       +0.00    -30.28 -142.81 22.71
  I live + $30 x2 after a loss         +705.69  +63.12   +46 / +17      +32.99   -28.40 -169.34 24.57
Mechanism: the trades after a loss are the best ones (Sat-Mon the next trade after a loss
won 36 of 39; 137: lost 17% vs 23% priced), so stake follows them. Per dollar staked: live
30.9, F 36.7, B 41.1, D 34.2, I 28.7. F beats live in both halves with a LOWER average stake;
I makes the most but by staking more. Best of 10 variants on the same 13 days - in-sample.
VERIFIED (wf_93e6a6dc-66b, independent replay): LIVE +642.58, F +671.43, B +636.88, I +705.69 -
to the cent. The 20% stop never fired, so all variants take the same 1,070 trades and differ
only in stake. F vs LIVE: ahead on 7 of 13 days; the daily gap's SD ~$37 -> a 13-day noise
band of about +/-$134: F's +28.85 (and its +138.85 on 10-03..10-05, after -110.00 on
09-23..10-02) is inside the noise. F risks ~12% less per trade; B ~26% less for the same money;
I's lead is from bigger stakes. Winning days: LIVE 10/13, F 11/13, B 11/13, I 10/13.
OPERATOR: "I want I, but test it on a $20 base, then after a loss $30 - and test after 1
loss and after 2 losses." scratchpad/trendfix/boost_i20.py, same engine, 13 days to 12:4x
(LIVE now +651.62 with today's later windows); $30 for the next 2 taken trades:
  I   $25/$10 + $30 x2 after 1 loss      +716.59  +64.98  halves +46 / +19  Sat-Mon +34.85  worst -28.40  avg $24.58  7-8/5
  I20 $20/$10 + $30 x2 after 1 loss      +661.62  +10.00  halves +21 / -11  Sat-Mon +69.86  worst -20.30  avg $22.68  7/6
  I20 $20/$10 + $30 x2 after 2 losses    +553.92  -97.69  halves  +3 / -100 Sat-Mon  +4.72  worst -14.63  avg $18.67  4/9
  I   $25/$10 + $30 x2 after 2 losses    +674.79  +23.17  halves +49 / -26  Sat-Mon  +2.20  worst -26.08  avg $21.19  6/5
  $20/$10 with no boost                  +484.17  -167.45                                   worst -22.92  avg $18.04  2/11
  F   $10 + $30 x2 after 1 loss          +682.33  +30.71  halves +18 / +13  Sat-Mon +140.71 worst -37.30  avg $18.29  7/6
The boost belongs right AFTER THE FIRST LOSS (that trade is the cushioned one): waiting for two
losses gives most of it away. At a $20 base the after-1-loss boost is worth ~+177 over no boost
and lands level with live (+10, inside the ~+/-$134 noise) with a smaller worst day.
OPERATOR: "Now test I on $25 and $30 after 1 loss twice, but only ..." (message cut off;
three readings tested - scratchpad/trendfix/boost_i_only.py; 13 days, LIVE +663.61):
  I $25/$10 + $30 x2 after 1 loss          +728.59  +64.98   halves +46 / +19   days 8/5   avg $24.58
  ... only BELOW THE 8% TARGET             +788.66  +125.05  halves +113 / +12  days 11/2  avg $21.52
  ... only while the day is red            +753.67  +90.06   halves +68 / +22   days 10/2  avg $20.94
  ... only after the day's first loss      +671.88  +8.27    halves +12 / -3    days 7/6   avg $20.63
Best so far: the $30 pair only below the 8% target - +125 on 11 of 13 days with ~4% more
staked per trade than live (worst day -28.40). Chosen after ~20 variants today on the same
days: independent recompute running (wf_7b4cc7d9-5f7) before any recommendation.
VERIFIED (wf_7b4cc7d9-5f7, independent replay to 12:30): differences vs LIVE identical -
I +64.98 (8/5), I_BELOW +125.05 (11/2; halves +112.96 / +12.09), I_RED +90.06 (10/2). All
take the same 1,071 trades. The $30 trades under I_BELOW: 306, won 83.3% at 75.7c (+7.6 points
over price) vs all trades 77.0% at 74.5c (+2.5) - the after-a-loss trades are the best ones.
Daily gap +9.62 mean, SD 16.6, SE 4.6 (~2.1 SE) - but chosen after ~20 variants on these days;
09-24 and 09-28 give 79.47 of the 125.05; second half only +12.09 (5/2), where I_RED (+22.16)
leads. Promising, not proven; the operator's decision.

## 161. The mirrors under the after-a-loss boost (2026-10-05 12:53)

OPERATOR: "Before that, how will the mirrors follow this - find their own mirror setup as
well." The mirrors copy the primary's taken trades (the same trades whatever the primary's
stake) at their own stake and pause at their own target; WITHOUT a mirror rule they would
copy the boosted trades at their normal stake. scratchpad/trendfix/mirror_boost.py, 13 days,
openings of 10-05, boost = the 2 copied trades after the mirror's own copied loss:
                           total   vs now  targets hit  worst day  deepest in a day (of balance)
  Wife $3 on 37.93   now   +54.57  -       9/13         -4.54      -12.02 (-32%)
                     $4 x2 +60.96  +6.39   10/13        -4.55      -14.38 (-38%)
                     $6 x2 +70.87  +16.30  11/13        -4.16      -19.85 (-52%)
  George $2 on 29.39 now   +34.47  -       9/13         -1.84       -7.17 (-24%)
                     $3 x2 +40.53  +6.06   11/13        -1.04       -9.63 (-33%)
                     $4 x2 +43.72  +9.25   11/13        -1.04      -11.98 (-41%)
  Affoue $2 on 119.51 same money as George; deepest -6% now, -8% at $3 x2, -10% at $4 x2.
A mirror pauses at its target, so "only below the target" holds by itself. The boost adds
~+$6 per mirror over 13 days and lifts target hits 9 -> 10-11 of 13, but deepens the intraday
dip - heavy for the small accounts (Wife -38%, George -33% at +$1), light for Affoue (-8%).
Primary boost patch written (scratchpad/patch_after_loss_boost.py), NOT applied - awaiting the
operator's mirror choice.

## 162. What the $10 above the 8% target looks like (2026-10-05)

OPERATOR: "You need to show me what $10 above our target looks like first" (before the
after-a-loss boost goes live). scratchpad/trendfix/above_target.py, audited engine, 13 days
09-23..10-05, opening 995.93, the rules being implemented: below 8% $25 ($30 for the 2 trades
after a loss), at/above 8% $10; a $10 loss can drop the day back under 8% -> $25 again.
9 of 13 days reached 8% (+79.67), between 03:15 and 18:00. AFTER the first hit:
  538 more trades: 351 at $10 won 258 (73.5%) at 74.1c -> -102.12 (-0.29 per trade);
  187 at $25/$30 after slipping back under 8% -> +97.00.  The after-hit part: -5.12 in all,
  per day from +43.61 (09-25) to -82.66 (10-02); no day reached the 20% stop (best +142.59).
Choices once the day reaches 8%:  $10 (being implemented) +788.66, 2 losing days;
  $10 + the $30 boost above 8% too -60.07; $25 -134.30; STOP at 8% +5.12 (1 losing day:
  10-02 +82.64 instead of -0.02; worst day the same -28.40, 10-04, which never reached 8%).
Reading: on this record the $10 part of the day is break-even - it neither adds nor costs
money, it adds swing. STOP-at-8% = same money, steadier (+5 is noise, 9 days). The pre-hit
win rate (79.8%) is inflated by selection (days reach 8% by winning) - not evidence the
signals get worse after the target. Earlier pause-at-target rejection (less money) was under
the old stakes; with the boost it is now a tie. Already on the 2,000-signal review list.

## 163. The operator's new setup: primary done at 8%, mirrors +$1 after a loss, Affoue trades on (2026-10-05)

OPERATOR (after FINDINGS 162): "Mirrors: boost all three by $1 // and stop for the day at
8% while we make one more change to Affoue mirror account that account become the account
that keep trading after target hit, and for that account set it to $6 base and $8 after 1
loss and the after target hit $3." With the earlier "implement I $30 boost only below 8%".
THE RULES:
  PRIMARY  $25; $30 for the 2 taken trades after a known loss; DONE for the day at 8%
           (DAILY_PROFIT_STOP_RATE=0.08 = its target; the $10 phase is gone).
  WIFE     $3 (scaled stake), $4 for the 2 after a loss; pauses at her own target as before.
  GEORGE   $2, $3 for the 2 after a loss; pauses at his own target as before.
  AFFOUE   $6, $8 for the 2 after a loss; at/above ITS OWN target (min(15%, 7 wins at $6)
           = $14.00 on 119.51) $3, and NOT paused there - it trades on to midnight.
  Once the primary is done for the day, its $ signals still go to every mirror whose own
  day takes an entry (below its target, or past it at a lower stake); the primary's row
  is booked 'copied' (no fill, never graded, in no money report) and the cushion, the
  after-2-losses trend skip and the after-a-loss stakes read it - its result from the
  alert's prediction on the same side - so the rules run on exactly as if the primary
  were still trading. A primary blocked for anything other than its cap copies nothing.
INTERPRETATION (said to the operator): Affoue's "$3 after target hit" is at ITS OWN target,
as the primary's $10 was at its own; "$8 after 1 loss" is for the 2 trades after it, as the
primary's $30. Wife and George would also copy past the primary's stop until their own
target - on the record they always reached it first (0 such trades in 13 days).
THE RECORD (scratchpad/trendfix/new_setup.py; 13 days 09-23..10-05, 1,073 taken trades):
  primary  +802.82, 9/13 days at 8%, worst day -28.40
  Wife     +62.02 (vs +55.63 now), targets 10/13 (9), deepest -14.38 (-38%; -32% now)
  George   +40.53 (vs +35.00), targets 11/13 (9), deepest -9.63 (-33%; -24% now)
  Affoue   +165.57 (vs +35.00 at $2 pausing), worst day -6.62, deepest -30.41 (-25% of 119.51)
           of which the trades after the primary was done: -19.27 over 9 days (538 trades,
           mostly at $3) - the same break-even-to-negative tail as the primary's $10 (FINDINGS 162).
  Other reading ($3 once the PRIMARY is done): Affoue +181.21, same worst day and dip.
REVIEW (wf_83445e67-f9f: 4 reviewers by lens + 5 adversarial verifiers; all 5 checked
findings REAL, medium; none could lose money directly). Fixed before deploy:
  1. The trade message's "Mirrors enabled" dropped Affoue once its target latched though it
     trades on at $3 -> mirror_label keeps an account with an after-target stake.
  2. A signal was booked 'copied' before any mirror answered: a missed copy counted as a
     taken trade (wrong boost / cushion / streak) and was never retried or chased. Now each
     copy answers back (a future per mirror, 30 s): 'copied' only if a mirror bought; else
     'unfilled' under order id COPY_MISSED_ID, which the 60 s retry and the 93c chase
     re-send through the same copy path at the moved price, as for the primary's own miss.
  3. Copied windows said nothing on Telegram: now COPIED TO THE MIRRORS (accounts, contracts,
     stakes), COPY WON/LOST (the market's result) and COPY NOT FILLED - never a $ figure (a
     mirror's money is its broker balance in the session summary).
  4. The DAILY CAP note was fixed at startup: now built when said, from who still trades.
  5. Pre-funding covered the day's stake only: now the after-a-loss stake ($30, +$1, $8).
  Known and accepted: the cash-out (OFF since 10-05) would not reach positions the mirrors
  open after the primary is done - fix before ever switching it back on.
  Verified: filled rows read EXACTLY as before (658 window probes on a copy of the live db
  + 4 altered copies: 0 differences); every live $ row has a prediction on the same side.
Tests: tests/test_new_setup_1005.py (real Store / DailyProfitGuard / MirrorTarget / _Mirror /
MirroringExecutionClient; mirror order checks run the real guard); mutation check below.

## 164. Study vs live on 10-03, and every day under the new live rules (2026-10-05)

10-03 AT $25: the study said +32.54, live -4.29 (scratchpad/trendfix/gap_1003.py). Window by
window: 25 trades both held agree within $0.64; 32 trades live CASHED OUT early made $21.76
less than holding; 3 live MISSES the study counted as bought were worth +$15.62. Both causes
were changed on 10-05 (cash-out OFF, the chase ON), so the study now models live closely -
but it still assumes every miss is bought and every price is the signal's ask.
Every day under the rules live since 10-05 14:30 (daily_new_live.py, openings of 10-05):
primary +775.12 over 13 days (9/13 done at 8%, 2 losing days: 10-04 -28.40, 10-05 so far),
Wife +59.12 (10/13 targets), George +40.53 (11/13), Affoue +158.63, all four +1,033.40.
Deepest intraday dips: primary -126.20 (09-28, still finished +84.74), Affoue -30.41.

## 165. Is the learning learning anything? (audit, 2026-10-05, wf_5b46f1ef-0ab)

NO ACTIONABLE LEARNING. BTC: 54 training runs 09-23..10-05, all ok, 0 promoted, 0 changed a
single decision (new_changes = 0, holdout delta +0.0000 every run); all 31,165 intelligence
verdicts 'neutral'; active policy 29 arms, vetoes and admissions OFF. Why: training and
validation are 100% the old historical corpus (cutoff 08-26, 40 days old; 6,429 corpus vs
944 live markets, live only in the newest 20% slice); the corpus no longer grows, so the
cutoff creeps ~3 h per run - live data reaches validation ~10-12, training ~mid-November. The
2 candidates examined each run flip sign train -> validation (bd10-15/bd15+, px70-85, mom5+).
Its confidence label (not used for size) forecasts WORSE than the ask (Brier 0.2205 vs 0.2162,
n=8,054); its strongest claim failed live (predicted -9.6c, live +2.4c +-4.4c, n=470 windows).
The similar-markets shadow: following ENTER NOW only would have given up 12.79/contract vs
taking every signal (1,249 windows); its PASS windows made money. Candidates vs all-signal
since 09-28: 2 of 3 would have lost money; c80b34f84 points right on 2 days only.
Other instances: same; ETH promoted 09-25..27 and SOL vetoed 09-26..10-02 (114 windows blocked,
64% of them winners) - all neutral now; BNB has no learning. Nothing here touches live money:
the all-signal order path never consults it and the main strategy it could gate is paused.

## 166. Every crypto under the live rules (2026-10-05, scratchpad/trendfix/crypto_live_rule.py)

Each instrument's own recorded primary signals and Kalshi reference, the BTC live rules ($25,
$30 x2 after a loss, cushion, after-2 trend skip, held, done at 8% of 995.93):
  BTC  14 d  1,295 sig  75.6% at 73.2c  edge +2.4c   +810.18  10/14 at 8%, 2 losing days, worst -55.05
  ETH  12 d  1,061      74.6% at 74.4c       +0.2c     -2.76   7/12, 5 losing, worst -135.54, low -327.61
  SOL  11 d    970      74.6% at 73.6c       +1.0c   -293.55   6/11, 4 losing, worst -331.25
  XRP  10 d    890      70.8% at 73.5c       -2.7c  -1010.88   4/10, 5 losing, worst -434.74
  NEAR 10 d    878      71.9% at 74.6c       -2.7c   -996.44   3/10, 7 losing, worst -442.56
  BNB   5 d    402      68.9% at 71.9c       -3.0c   -717.12   1/5,  4 losing, worst -566.35
Only BTC wins more often than its price. The others reach 8% on half their days but their bad
days are 3-10x BTC's. Without the cushion and trend skip every one is worse (BTC +659.91).

## 167. The hourly edge search, all parameters (2026-10-05, wf_d65ca6f7-31a) - NO EDGE

Operator: "find the edge on the hourly, run all parameters optimization, even if it implies a
combo". Five families, ~487,600 configurations, search on the oldest 60% of hours, best tested
once on the newest 40%, best-of-N luck checks, net of fees at the ask, one entry per hour,
Kalshi only (scripts + outputs: scratchpad/hourly_edge/<family>/).
  A history grid (217,800 cfg; 1,517 hours 07-15..09-21 + 303 live): best +10.7c -> holdout
    +2.45c [-5.6, +10.3], live +1.8c (n=21); luck p 0.985 (weaker than noise).
  B live quotes + BRTI (245,970): best (Europe longshots vs momentum) +16.8c -> holdout 0/27 won,
    -5.2c. Lead only: "Z15 pullback" holdout +14.3c n=32 but fails the luck check.
  C our 15-min signal -> hourly (336): S1 = xx:15 signal, rung 3 steps in the money (~93c):
    search +4.8c, holdout +3.66c [-0.2, +6.4] n=73 / 6 days, luck p 0.14. Lead for a forward test.
  D cross-market 15m vs hourly (1,620): best +20.1c -> holdout +13.3c [-4.1, +31.3] n=32, luck p 0.585.
  E ladder shape / momentum / late certainty (21,861): longshots overpriced 1-4c both sides;
    favourites fair after fees; momentum/reversal nothing; 97-99c late = 0-loss streak, binomial p 0.14.
STRUCTURE (verified): KXBTCD and the KXBTC15M window closing at the same instant settle on the
IDENTICAL value (1,782/1,782 hours), so 15m + opposite hourly rung pairs pay >= $1 in every state.
On history candles the pair costs < $1 all-in in only ~3.6% of hours (~1.5c); the live "arbitrage"
(55-82 of 303 hours, ~5c) is a stale /markets-quote artifact - gone at the next poll 81-91% of the
time. Executability needs an hourly ORDER-BOOK recording in the last 15 minutes (not built).
METHOD: day sign-flip / day bootstrap overstate certainty on 0-loss favourites; use a binomial test.


## Live target change ? 2026-10-07 18:30 ET

Operator authorized 3% daily profit target for PRIMARY AND ALL MIRRORS; Affoue no longer trades past its target. DAILY_PROFIT_TARGET_RATE=0.03, DAILY_PROFIT_STOP_RATE=0.03, MIRROR_DAILY_PROFIT_TARGET_RATE=0.03, MIRROR_TARGET_MAX_WINS=0, ALLSIGNAL_AFTER_TARGET_STAKE=0 and MIRROR_1/2/3_ALLSIGNAL_AFTER_TARGET_STAKE=0. Stakes and after-loss sizing unchanged. Each account pauses until midnight New York after its own target; shadow recording continues. Mirror-after-primary-done remains enabled only so a mirror below its OWN target may continue until reaching it.

Today opening capital and existing pause/cap timestamps preserved. Today's target fields updated to opening*0.03 (primary $19.6806; Wife $1.0431; George $0.8814; Affoue $1.4595). All four had already crossed their targets today; Affoue's historical pause is retained even though its later all-day trading brought today's result below target.

90 relevant tests passed (daily profit, primary cap, recording never pauses, new setup). Broker flat on all four accounts, no resting orders; restart preflight passed. BTC PID 18388 replaced by watchdog PID 22564; fresh observations verified on BTC/ETH/GOLD/SILVER/SOL/XRP/NEAR/BNB. No shadow service restart, no day-capital reset. Backup .env.bak-20261007-3pct; prior day state runtime/3pct_change_20261007_before.json.
