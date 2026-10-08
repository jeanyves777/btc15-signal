# October 6 fakeout study

Read-only analysis of 1,392 real recorded BTC signals, September 23 through October 7 at 18:15 ET. Live settings unchanged.

## Entry-time detectors

- Low progress: absolute net price movement / total minute-to-minute movement <= 0.25 over the last 15 minutes.
- Strike whipsaw: at least three crossings of the current contract strike across the last 15 minutes of sampled prices.
- Combined: both conditions.
- Broad lock: either condition, released if two minute snapshots clear a fixed preceding 30-minute range in the signal direction by 2 basis points. This is a stateless entry test, not a persistent breakout state machine.

Kalshi BRTI samples require source and received timestamps no later than the sampled time, with maximum 60-second staleness. No future prices or outcomes enter the filter. Missing history affects 28 signals; those are kept, not silently removed. Minute snapshots are not OHLC candles; this study does not test ADX or volume confirmation.

## Actual October 6 lifecycle attribution

86 recorded live trade lifecycles matched to signals preceding creation. Their broker-derived net currently totals -$251.9799. This differs from the older -$231.58 figure; the reason for the revision is not established here. Do not treat the older figure as the current event sum.

| Filter | Flagged actual losses | Flagged actual wins | Net of flagged trades |
|---|---:|---:|---:|
| low_progress | 12 | 24 | $-111.62 |
| strike_whipsaw | 14 | 25 | $-156.49 |
| both | 9 | 10 | $-128.81 |
| either_with_breakout_release | 16 | 39 | $-114.01 |

Removing those outcomes is attribution only: later stake changes, fills and daily pauses would also change. These are not promised savings or a faithful replay of the entire live strategy.

## Controlled signal comparison

Fixed $25 stake per signal, observed ask, estimated fees, held to recorded settlement. No daily cap, loss boost, chase or cashout. This isolates the detector; figures differ from actual account profit.

| Filter | Oct 6 modeled net | All 15 days modeled net | Before Oct 6 change vs baseline |
|---|---:|---:|---:|
| baseline | $-369.50 | $-52.82 | $+0.00 |
| low_progress | $-99.57 | $+522.29 | $+158.05 |
| strike_whipsaw | $-175.81 | $+371.11 | $+104.75 |
| both | $-173.11 | $+642.75 | $+353.60 |
| either_with_breakout_release | $-127.45 | $+167.71 | $-110.20 |

## Interpretation

Both warnings together are a candidate for further shadow evaluation: they improved this retrospective sample while blocking fewer winners than the broad lock. No tested detector eliminated October 6 losses. The broad lock reduced October 6 losses but harmed the earlier-period comparison. Thresholds were explored after observing this episode: none of these results is independent validation.

The current recorded sequence first crosses 3% of opening $908.00 at 2026-10-06T02:45:07.141000-04:00, with $+27.59. That daily pause is separate from detecting choppiness.

The screenshot illustrates another venue and does not prove why every Kalshi trade lost. A faithful combined 3%-cap plus filter replay, with actual outcome availability and account sizing, remains necessary before considering deployment.