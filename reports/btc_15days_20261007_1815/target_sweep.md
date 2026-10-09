# Lower daily-profit targets: same 15-day record

Only the daily stop threshold changes. Fixed $995.93 opening, same recorded signals/polls/outcomes and $25/$30 stakes as the preceding study. Each lower-target run is an exact prefix of the saved 8% run: no missing after-stop trades are needed to evaluate a LOWER stop. Fees already included. Counterfactual fills remain assumed at observed asks; this is not actual account profit.

| Target | Dollar target | 15-day modeled net | Change vs 8% | Target days | Losing days | Oct 6 net |
|---|---:|---:|---:|---:|---:|---:|
| 2% | $19.92 | $+317.82 | $-280.90 | 14/15 | 1 | $+25.66 |
| 3% | $29.88 | $+441.39 | $-157.33 | 13/15 | 1 | $+33.79 |
| 4% | $39.84 | $+477.36 | $-121.36 | 12/15 | 2 | $+44.90 |
| 5% | $49.80 | $+621.36 | $+22.64 | 12/15 | 2 | $+50.53 |
| 6% | $59.76 | $+414.64 | $-184.08 | 11/15 | 3 | $-231.58 |
| 7% | $69.72 | $+502.64 | $-96.08 | 10/15 | 3 | $-231.58 |
| 8% | $79.67 | $+598.72 | $-0.00 | 10/15 | 3 | $-231.58 |

## Actual October 6 broker-derived event sequence

Opening $908.00; 86 events; sum $-251.98, matching the stored daily-profit record. These are retrospective prefixes of actual net events, not quote fills. Event timing uses realised_ms; exact guard polling/notice delay is not replayed. Event amounts may reflect later broker reconciliation. No fee subtraction was added.

| Target | Dollar target | Recorded P&L at crossing or final | First crossing ET |
|---|---:|---:|---|
| 2% | $18.16 | $+19.17 | 2026-10-06T00:30:07.213000-04:00 |
| 3% | $27.24 | $+27.59 | 2026-10-06T02:45:07.141000-04:00 |
| 4% | $36.32 | $+36.63 | 2026-10-06T04:30:07.138000-04:00 |
| 5% | $45.40 | $-251.98 | Never reached |
| 6% | $54.48 | $-251.98 | Never reached |
| 7% | $63.56 | $-251.98 | Never reached |
| 8% | $72.64 | $-251.98 | Never reached |

Already-open positions at each crossing are listed in target_sweep.json; a daily stop prevents new entries, not exits.

This comparison selects thresholds on the same historical sample, so a better result here is not independent evidence of future superiority. No live change or service restart was made.