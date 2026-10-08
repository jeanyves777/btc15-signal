# October 6 regime, fakeout and daily-target investigation

Completed offline using September 23–October 7 windows closing through 18:15 ET, with their eventual recorded outcomes. This is the same frozen 15-day interval used before; it does not include October 8. No production changes or restarts.

## Finding

The afternoon loss episode is consistent with local directional signals being taken inside a larger post-shock range. In the source audited at the start of this study, regime labels did not enforce a restriction on the all-signal entry path. A specific post-shock contraction filter is promising in the observed-quote replay, but its 8% result is sensitive to thresholds and execution costs. This study does not establish a dependable cure or justify changing live targets.

The earlier claim that -$231.58 changed to -$251.98 was incorrect: the former was the model and the latter actual broker-derived net. Both contain the SAME 86 October 6 windows. Actual gross -$213.40982 minus $38.57008 entry fees equals -$251.97990; no cashouts. Execution/accounting assumptions explain the $20.4027 gap, not extra/missing trades.

## Actual October 6 loss timing

| ET interval | Actual trades | W | L | Actual net |
| --- | --- | --- | --- | --- |
| 00–11 | 41 | 32 | 9 | $+7.97 |
| 11–15 | 15 | 11 | 4 | $-3.55 |
| 15–21 | 18 | 8 | 10 | $-234.73 |
| 21–24 | 12 | 9 | 3 | $-21.67 |

The screenshot is BTCUSDT perpetual on another venue. Its shape is consistent with the archived reference path, but its exact price/volume/candle values are not substituted for Kalshi. Archived brti_value is a smoothed BRTI reference, not raw exchange ticks.

## What could have been seen before entry

At 19:49 ET the UP signal had strong 15-minute directional efficiency (~0.93) while hourly efficiency was ~0.002: nearly all of the hour's movement had cancelled out. It was still inside the prior hourly range. This is why a short trend test can accept a false breakout. Another losing signal around 19:34 had two minute samples below the preceding range; even two confirmations can fail.

The proposed slow ADX/CHOP/MA vote switched into chop at 16:00–19:20 and 20:00–21:35, but it was off from 12:00–15:55. A persistent lock remembers the range through short changes in indicator readings. Existing vol_regime="low" occurred on 85 of 86 actual trades that day and is not a useful discriminator by itself.

Post-shock candidate tested (chosen after inspecting this event):

1. A one-hour reference move of at least 1% occurred within the last 8 hours.
2. The recent hour's range is at most half the range during that shock hour.
3. Hourly directional efficiency is <=0.25.
4. Two completed minute samples have not both cleared the preceding range in the signal direction by 2 basis points.

The shock can be up or down. Range boundaries exclude the two confirmation samples. This candidate is an entry-time test, not an instruction to trade a breakout. Missing data passes through and is counted.

## Main comparison: all 15 days

Modeled net, fixed $995.93 opening each day (no compounding), $25 base / $30 after a known loss for two taken trades, 5bps cushion and known-two-loss trend skip from the policy captured at study start, hold to settlement, fees included. Fills at observed asks are hypothetical. Target halts new entries once known realized profit crosses it. Max DD below is the largest intraday realized peak-to-trough drawdown, not mark-to-market drawdown.

| Entry rule | Target | 15-day net | Worst day | Max intraday DD | Target days | Oct 6 net |
| --- | --- | --- | --- | --- | --- | --- |
| Existing entries | 3% | $+463.77 | $-23.10 | $121.51 | 13/15 | $+33.79 |
| Existing entries | 5% | $+638.67 | $-36.31 | $143.98 | 12/15 | $+50.53 |
| Existing entries | 8% | $+603.21 | $-231.58 | $296.43 | 10/15 | $-231.58 |
| Post-drop/rally range contraction | 3% | $+464.10 | $-23.10 | $116.11 | 13/15 | $+33.79 |
| Post-drop/rally range contraction | 5% | $+672.71 | $-23.10 | $116.11 | 12/15 | $+50.53 |
| Post-drop/rally range contraction | 8% | $+716.26 | $-23.10 | $150.65 | 7/15 | $-9.18 |
| Persistent ADX/CHOP/MA lock | 3% | $+490.82 | $-12.43 | $121.51 | 14/15 | $+30.71 |
| Persistent ADX/CHOP/MA lock | 5% | $+692.51 | $-12.43 | $121.51 | 13/15 | $+56.37 |
| Persistent ADX/CHOP/MA lock | 8% | $+546.45 | $-143.93 | $259.02 | 7/15 | $-48.73 |
| 15m chop + strike crossings | 3% | $+276.95 | $-60.76 | $161.51 | 12/15 | $+29.89 |
| 15m chop + strike crossings | 5% | $+471.22 | $-60.76 | $161.51 | 11/15 | $+51.17 |
| 15m chop + strike crossings | 8% | $+742.76 | $-60.76 | $161.51 | 10/15 | $+83.02 |

The post-shock filter does not reach 8% on October 6: it reduces the modeled loss to -$9.18. It reaches 8% on only 7/15 days. Its 8% net improvement across the sample is +$113.05; excluding October 6 it is -$109.35 versus the corresponding baseline. Therefore its aggregate gain is dominated by this one event. At 5%, Oct 6 is already stopped before the afternoon in the observed-quote model; that does not mean the actual account achieved 5%.

## Execution and delay stress tests

Price stresses charge an extra 1c or 2c on every hypothetical entry, recompute whole-contract count, fees, sizes and target crossings. They are sensitivity scenarios, not estimates of actual slippage. The delay stress makes outcomes unavailable until at least 5 minutes after close.

| Rule | Target | Stress | Net | Worst day | Max intraday DD |
| --- | --- | --- | --- | --- | --- |
| Existing entries | 5% | >=5min outcome delay | $+356.90 | $-234.19 | $305.85 |
| Existing entries | 5% | +1c entry | $+175.66 | $-259.62 | $307.44 |
| Existing entries | 5% | +2c entry | $+57.10 | $-284.62 | $322.84 |
| Existing entries | 8% | >=5min outcome delay | $+551.12 | $-234.19 | $305.85 |
| Existing entries | 8% | +1c entry | $+306.99 | $-259.62 | $307.44 |
| Existing entries | 8% | +2c entry | $-331.48 | $-284.62 | $322.84 |
| Persistent ADX/CHOP/MA lock | 5% | >=5min outcome delay | $+635.35 | $-15.35 | $115.23 |
| Persistent ADX/CHOP/MA lock | 5% | +1c entry | $+443.95 | $-66.96 | $146.92 |
| Persistent ADX/CHOP/MA lock | 5% | +2c entry | $+372.12 | $-83.70 | $149.01 |
| Persistent ADX/CHOP/MA lock | 8% | >=5min outcome delay | $+496.57 | $-138.93 | $253.52 |
| Persistent ADX/CHOP/MA lock | 8% | +1c entry | $+166.79 | $-165.64 | $265.98 |
| Persistent ADX/CHOP/MA lock | 8% | +2c entry | $-195.72 | $-185.27 | $272.02 |
| Post-drop/rally range contraction | 5% | >=5min outcome delay | $+617.90 | $-36.66 | $119.20 |
| Post-drop/rally range contraction | 5% | +1c entry | $+472.61 | $-49.98 | $125.64 |
| Post-drop/rally range contraction | 5% | +2c entry | $+235.61 | $-75.67 | $139.55 |
| Post-drop/rally range contraction | 8% | >=5min outcome delay | $+759.83 | $-36.66 | $121.16 |
| Post-drop/rally range contraction | 8% | +1c entry | $+360.14 | $-52.94 | $194.88 |
| Post-drop/rally range contraction | 8% | +2c entry | $-123.29 | $-75.67 | $199.58 |

In particular, the post-shock 8% replay falls from +$716.26 to -$123.29 with 2c worse entries. Its 5% counterpart remains +$235.61 in that scenario, but can still suffer meaningful drawdowns. A profit target does not cap losses before the target is reached.

## Threshold sensitivity: post-shock candidate

Every nearby setting tested is shown. No replacement threshold was selected from these results.

| Shock | Lookback h | Range ratio | Target | Net | Worst day |
| --- | --- | --- | --- | --- | --- |
| 75bps | 8 | 0.5 | 5% | $+380.11 | $-151.50 |
| 75bps | 8 | 0.5 | 8% | $+466.11 | $-151.50 |
| 125bps | 8 | 0.5 | 5% | $+560.43 | $-88.42 |
| 125bps | 8 | 0.5 | 8% | $+332.69 | $-231.58 |
| 100bps | 4 | 0.5 | 5% | $+651.87 | $-23.10 |
| 100bps | 4 | 0.5 | 8% | $+629.21 | $-178.52 |
| 100bps | 12 | 0.5 | 5% | $+682.80 | $-23.10 |
| 100bps | 12 | 0.5 | 8% | $+676.28 | $-70.67 |
| 100bps | 8 | 0.4 | 5% | $+705.53 | $-23.10 |
| 100bps | 8 | 0.4 | 8% | $+695.20 | $-81.66 |
| 100bps | 8 | 0.6 | 5% | $+634.76 | $-23.10 |
| 100bps | 8 | 0.6 | 8% | $+674.54 | $-26.10 |
| 100bps | 8 | 0.5 | 5% | $+672.71 | $-23.10 |
| 100bps | 8 | 0.5 | 8% | $+716.26 | $-23.10 |

Changing the shock threshold from 100bps to 125bps restores a -$231.58 worst day at an 8% target. This is a material fragility, not a minor parameter preference.

## Other afternoons: actual and modeled comparisons

Actual net uses only archived matched lifecycle events; early dates may have no actual all-signal trades. The modeled afternoon numbers below have NO daily cap, so they test the filter after an early target would otherwise hide the afternoon. They recompute taken-trade sizing and cushion decisions through the full day. They are not account profits.

| Day | Actual trades | Actual 15–21 net | Baseline modeled | Post-shock modeled | Persistent-lock modeled |
| --- | --- | --- | --- | --- | --- |
| 2026-09-23 | 0 | Not available | $-66.64 | $+16.25 | $-46.73 |
| 2026-09-24 | 0 | Not available | $-27.53 | $-70.58 | $-33.30 |
| 2026-09-25 | 0 | Not available | $-22.47 | $-76.58 | $+0.00 |
| 2026-09-26 | 0 | Not available | $-52.79 | $-52.79 | $-12.73 |
| 2026-09-27 | 0 | Not available | $-51.35 | $-51.35 | $+37.86 |
| 2026-09-28 | 23 | $+1.47 | $+45.39 | $-3.27 | $+68.59 |
| 2026-09-29 | 16 | $-7.35 | $-64.83 | $-41.17 | $-32.55 |
| 2026-09-30 | 23 | $-9.28 | $-57.57 | $+12.28 | $-54.15 |
| 2026-10-01 | 23 | $-0.74 | $-1.72 | $+38.45 | $+7.84 |
| 2026-10-02 | 20 | $+20.21 | $+77.13 | $-27.25 | $-10.83 |
| 2026-10-03 | 14 | $+9.70 | $+39.25 | $+39.25 | $-21.05 |
| 2026-10-04 | 21 | $+42.96 | $+80.70 | $+80.70 | $+50.88 |
| 2026-10-05 | 24 | $-44.01 | $-41.63 | $+2.20 | $-53.76 |
| 2026-10-06 | 18 | $-234.73 | $-234.50 | $-28.30 | $-51.80 |
| 2026-10-07 | 0 | Not available | $-8.26 | $-8.26 | $+0.00 |

## Coverage and verification

- 1392 signals, all matched to official outcomes; no synthetic signals/outcomes.
- Outcome timing: {'shadow_official_arrival': 1007, 'observed_grade': 385}. Shadow official arrival may lag the main service; it is observed arrival, not assumed knowledge at expiry.
- Missing features: 15-minute efficiency 14; hourly efficiency 50; full 5-minute indicator warmup 246. Post-shock missing current/shock histories: 50 / 31.
- All price features require source and receipt timestamps no later than the evaluated time. Completed bars only; no volume confirmation is claimed.
- Replay covers unknown prior outcome waits, late winning-result release, loss streaks, loss boosts, daily target checks during waits, and daily sequence reset.
- Delayed-entry regime rechecks were run separately: post-shock main results were unchanged. Persistent-lock and short-chop results differ, and are included in results.json.
- Six automated checks cover delayed data, unknown results, late-win cushion release, target crossing during a wait, skipped-trade boost state, and indicator gap/causality handling.
- Live retries/chase, partial fills, liquidity, rejected orders, exact broker polling and cash balance constraints are not replayed. Thus this is still not a faithful broker execution simulation.
- Actual history has changing stakes/rules; modeled dollars use one fixed opening and current base/boost policy. Both are labeled separately.
- All days were already inspected; the first 10/last 5 split is descriptive, not a genuinely untouched holdout. Candidates and threshold exploration are hindsight research.

## Full candidate results

| Rule | Target | Net | Worst day | Max intraday DD | Target days |
| --- | --- | --- | --- | --- | --- |
| Existing entries | 3% | $+463.77 | $-23.10 | $121.51 | 13 |
| Existing entries | 5% | $+638.67 | $-36.31 | $143.98 | 12 |
| Existing entries | 8% | $+603.21 | $-231.58 | $296.43 | 10 |
| Existing entries | No cap | $+321.71 | $-231.58 | $296.43 | 0 |
| 15m chop + strike crossings | 3% | $+276.95 | $-60.76 | $161.51 | 12 |
| 15m chop + strike crossings | 5% | $+471.22 | $-60.76 | $161.51 | 11 |
| 15m chop + strike crossings | 8% | $+742.76 | $-60.76 | $161.51 | 10 |
| 15m chop + strike crossings | No cap | $+753.09 | $-121.07 | $232.23 | 0 |
| Blanket hourly chop | 3% | $-90.06 | $-179.65 | $187.29 | 11 |
| Blanket hourly chop | 5% | $+52.92 | $-179.65 | $187.29 | 8 |
| Blanket hourly chop | 8% | $-83.61 | $-179.65 | $187.29 | 5 |
| Blanket hourly chop | No cap | $-60.38 | $-179.65 | $187.29 | 0 |
| ADX/CHOP/MA vote | 3% | $+201.70 | $-183.88 | $209.47 | 13 |
| ADX/CHOP/MA vote | 5% | $+366.97 | $-183.88 | $209.47 | 9 |
| ADX/CHOP/MA vote | 8% | $+315.78 | $-183.88 | $259.02 | 7 |
| ADX/CHOP/MA vote | No cap | $+118.57 | $-183.88 | $259.02 | 0 |
| Persistent ADX/CHOP/MA lock | 3% | $+490.82 | $-12.43 | $121.51 | 14 |
| Persistent ADX/CHOP/MA lock | 5% | $+692.51 | $-12.43 | $121.51 | 13 |
| Persistent ADX/CHOP/MA lock | 8% | $+546.45 | $-143.93 | $259.02 | 7 |
| Persistent ADX/CHOP/MA lock | No cap | $+264.46 | $-143.93 | $259.02 | 0 |
| Always reject opposing 15m trend | 3% | $+437.88 | $-29.20 | $113.96 | 12 |
| Always reject opposing 15m trend | 5% | $+440.11 | $-197.37 | $278.13 | 11 |
| Always reject opposing 15m trend | 8% | $+639.41 | $-197.37 | $278.13 | 9 |
| Always reject opposing 15m trend | No cap | $+433.21 | $-197.37 | $278.13 | 0 |
| 15m chop or opposing trend | 3% | $+295.75 | $-77.75 | $104.98 | 12 |
| 15m chop or opposing trend | 5% | $+531.92 | $-77.75 | $104.98 | 12 |
| 15m chop or opposing trend | 8% | $+734.03 | $-77.75 | $154.39 | 10 |
| 15m chop or opposing trend | No cap | $+818.13 | $-103.42 | $232.23 | 0 |
| Signal ask >=70c | 3% | $+350.85 | $-53.26 | $151.07 | 13 |
| Signal ask >=70c | 5% | $+544.47 | $-53.26 | $151.07 | 12 |
| Signal ask >=70c | 8% | $+470.88 | $-150.82 | $226.30 | 6 |
| Signal ask >=70c | No cap | $+532.91 | $-150.82 | $226.30 | 0 |
| No $30 loss boost | 3% | $+371.33 | $-43.77 | $131.95 | 12 |
| No $30 loss boost | 5% | $+296.73 | $-205.82 | $260.54 | 10 |
| No $30 loss boost | 8% | $+461.21 | $-205.82 | $260.54 | 8 |
| No $30 loss boost | No cap | $+205.88 | $-205.82 | $260.54 | 0 |
| Post-drop/rally range contraction | 3% | $+464.10 | $-23.10 | $116.11 | 13 |
| Post-drop/rally range contraction | 5% | $+672.71 | $-23.10 | $116.11 | 12 |
| Post-drop/rally range contraction | 8% | $+716.26 | $-23.10 | $150.65 | 7 |
| Post-drop/rally range contraction | No cap | $+622.32 | $-29.08 | $187.66 | 0 |

## Decision supported by this study

The missing protection is admission control for a price range after a large move; a label or an isolated short-momentum check is insufficient. The evidence supports testing a persistent range state and post-shock detector prospectively, with frozen settings, on the all-signal path including waits/retries. It does not support raising the live target to 8% now. Five percent is the more resilient research candidate in these stress tests, not a newly proven live rule. Collect an untouched forward comparison before treating the apparent improvement as established.

Reproduction: run study.py, test_study.py with pytest, then emit_report.py. All production SQLite connections use mode=ro. inputs.json.gz freezes the signal, poll, BRTI and actual-event inputs used; CSV files expose per-day and per-trade calculations. Source hashes are in results.json.
