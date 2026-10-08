"""Render the full comparison; never selects or hides losing candidates."""
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

P=Path(__file__).resolve().parent
R=json.loads((P/'results.json').read_text())
NY=ZoneInfo('America/New_York')
def time(ms):return datetime.fromtimestamp(int(ms)/1000,NY).strftime('%H:%M:%S')
def money(x):return f'${x:+,.2f}'
def get(rule,rate,period='all15'):
    return next(r for r in R['summary'] if r['rule']==rule and r['rate']==rate and r['period']==period)
def table(headers,rows):
    return ['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+['| '+' | '.join(map(str,r))+' |' for r in rows]
signals=list(csv.DictReader((P/'signals.csv').open(encoding='utf-8')))
actual=list(csv.DictReader((P/'actual_attribution.csv').open(encoding='utf-8')))
daily=list(csv.DictReader((P/'daily.csv').open(encoding='utf-8')))
names={'baseline':'Existing entries','chop_0.25_3':'15m chop + strike crossings',
       'hour_chop':'Blanket hourly chop','ohlc_chop':'ADX/CHOP/MA vote',
       'ohlc_lock':'Persistent ADX/CHOP/MA lock','trend_always':'Always reject opposing 15m trend',
       'chop_and_trend':'15m chop or opposing trend','ask_floor_70':'Signal ask >=70c',
       'no_loss_boost':'No $30 loss boost','postshock':'Post-drop/rally range contraction'}
lines=['# October 6 regime, fakeout and daily-target investigation','',
 'Completed offline using September 23–October 7 windows closing through 18:15 ET, with their eventual recorded outcomes. This is the same frozen 15-day interval used before; it does not include October 8. No production changes or restarts.','',
 '## Finding','',
 'The afternoon loss episode is consistent with local directional signals being taken inside a larger post-shock range. In the source audited at the start of this study, regime labels did not enforce a restriction on the all-signal entry path. A specific post-shock contraction filter is promising in the observed-quote replay, but its 8% result is sensitive to thresholds and execution costs. This study does not establish a dependable cure or justify changing live targets.','',
 'The earlier claim that -$231.58 changed to -$251.98 was incorrect: the former was the model and the latter actual broker-derived net. Both contain the SAME 86 October 6 windows. Actual gross -$213.40982 minus $38.57008 entry fees equals -$251.97990; no cashouts. Execution/accounting assumptions explain the $20.4027 gap, not extra/missing trades.','',
 '## Actual October 6 loss timing','']
oct6=[r for r in actual if r['day']=='2026-10-06']
rows=[]
for label,lo,hi in [('00–11',0,11),('11–15',11,15),('15–21',15,21),('21–24',21,24)]:
    ss=[r for r in oct6 if lo<=int(r['hour'])<hi]
    rows.append((label,len(ss),sum(float(r['net'])>0 for r in ss),sum(float(r['net'])<0 for r in ss),money(sum(float(r['net']) for r in ss))))
lines+=table(['ET interval','Actual trades','W','L','Actual net'],rows)
lines+=['', 'The screenshot is BTCUSDT perpetual on another venue. Its shape is consistent with the archived reference path, but its exact price/volume/candle values are not substituted for Kalshi. Archived brti_value is a smoothed BRTI reference, not raw exchange ticks.','',
 '## What could have been seen before entry','',
 'At 19:49 ET the UP signal had strong 15-minute directional efficiency (~0.93) while hourly efficiency was ~0.002: nearly all of the hour\'s movement had cancelled out. It was still inside the prior hourly range. This is why a short trend test can accept a false breakout. Another losing signal around 19:34 had two minute samples below the preceding range; even two confirmations can fail.','',
 'The proposed slow ADX/CHOP/MA vote switched into chop at 16:00–19:20 and 20:00–21:35, but it was off from 12:00–15:55. A persistent lock remembers the range through short changes in indicator readings. Existing vol_regime="low" occurred on 85 of 86 actual trades that day and is not a useful discriminator by itself.','',
 'Post-shock candidate tested (chosen after inspecting this event):','',
 '1. A one-hour reference move of at least 1% occurred within the last 8 hours.','2. The recent hour\'s range is at most half the range during that shock hour.','3. Hourly directional efficiency is <=0.25.','4. Two completed minute samples have not both cleared the preceding range in the signal direction by 2 basis points.','',
 'The shock can be up or down. Range boundaries exclude the two confirmation samples. This candidate is an entry-time test, not an instruction to trade a breakout. Missing data passes through and is counted.','',
 '## Main comparison: all 15 days','',
 'Modeled net, fixed $995.93 opening each day (no compounding), $25 base / $30 after a known loss for two taken trades, 5bps cushion and known-two-loss trend skip from the policy captured at study start, hold to settlement, fees included. Fills at observed asks are hypothetical. Target halts new entries once known realized profit crosses it. Max DD below is the largest intraday realized peak-to-trough drawdown, not mark-to-market drawdown.','']
rows=[]
for rule in ['baseline','postshock','ohlc_lock','chop_0.25_3']:
    for rate in [.03,.05,.08]:
        s=get(rule,rate);o=get(rule,rate,'oct6')
        rows.append((names[rule],f'{rate:.0%}',money(s['net']),money(s['worst_day']),f"${s['max_intraday_dd']:.2f}",f"{s['target_days']}/15",money(o['net'])))
lines+=table(['Entry rule','Target','15-day net','Worst day','Max intraday DD','Target days','Oct 6 net'],rows)
lines+=['', 'The post-shock filter does not reach 8% on October 6: it reduces the modeled loss to -$9.18. It reaches 8% on only 7/15 days. Its 8% net improvement across the sample is +$113.05; excluding October 6 it is -$109.35 versus the corresponding baseline. Therefore its aggregate gain is dominated by this one event. At 5%, Oct 6 is already stopped before the afternoon in the observed-quote model; that does not mean the actual account achieved 5%.','',
 '## Execution and delay stress tests','',
 'Price stresses charge an extra 1c or 2c on every hypothetical entry, recompute whole-contract count, fees, sizes and target crossings. They are sensitivity scenarios, not estimates of actual slippage. The delay stress makes outcomes unavailable until at least 5 minutes after close.','']
rows=[]
for r in R['execution_sensitivity']:
    if r['rule'] in ('baseline','postshock','ohlc_lock') and r['rate'] in (.05,.08):
        stress='>=5min outcome delay' if r['delay']==300 else f"+{r['slippage']*100:.0f}c entry"
        rows.append((names[r['rule']],f"{r['rate']:.0%}",stress,money(r['net']),money(r['worst_day']),f"${r['max_intraday_dd']:.2f}"))
lines+=table(['Rule','Target','Stress','Net','Worst day','Max intraday DD'],rows)
lines+=['', 'In particular, the post-shock 8% replay falls from +$716.26 to -$123.29 with 2c worse entries. Its 5% counterpart remains +$235.61 in that scenario, but can still suffer meaningful drawdowns. A profit target does not cap losses before the target is reached.','',
 '## Threshold sensitivity: post-shock candidate','',
 'Every nearby setting tested is shown. No replacement threshold was selected from these results.','']
lines+=table(['Shock','Lookback h','Range ratio','Target','Net','Worst day'],[(f"{r['shock_bps']}bps",r['hours'],r['ratio'],f"{r['rate']:.0%}",money(r['net']),money(r['worst_day'])) for r in R['postshock_sensitivity']])
lines+=['', 'Changing the shock threshold from 100bps to 125bps restores a -$231.58 worst day at an 8% target. This is a material fragility, not a minor parameter preference.','',
 '## Other afternoons: actual and modeled comparisons','',
 'Actual net uses only archived matched lifecycle events; early dates may have no actual all-signal trades. The modeled afternoon numbers below have NO daily cap, so they test the filter after an early target would otherwise hide the afternoon. They recompute taken-trade sizing and cushion decisions through the full day. They are not account profits.','']
led=list(csv.DictReader((P/'modeled_trades.csv').open(encoding='utf-8')))
rows=[]
for day in sorted({r['day'] for r in signals}):
    ac=[r for r in actual if r['day']==day and 15<=int(r['hour'])<21]
    vals=[]
    for rule in ['baseline','postshock','ohlc_lock']:
        ss=[r for r in led if r['day']==day and r['rule']==rule and float(r['rate'])==0 and 15<=int(time(r['at'])[:2])<21]
        vals.append(money(sum(float(r['net']) for r in ss)))
    rows.append((day,len(ac),money(sum(float(r['net']) for r in ac)) if ac else 'Not available',*vals))
lines+=table(['Day','Actual trades','Actual 15–21 net','Baseline modeled','Post-shock modeled','Persistent-lock modeled'],rows)
lines+=['', '## Coverage and verification','',
 f"- {R['window']['signals']} signals, all matched to official outcomes; no synthetic signals/outcomes.",
 f"- Outcome timing: {R['coverage']['timing']}. Shadow official arrival may lag the main service; it is observed arrival, not assumed knowledge at expiry.",
 f"- Missing features: 15-minute efficiency {R['coverage']['er15_missing']}; hourly efficiency {R['coverage']['er60_missing']}; full 5-minute indicator warmup {R['coverage']['ohlc_missing']}. Post-shock missing current/shock histories: {R['postshock_coverage']['coverage'].get('missing_current_hour',0)} / {R['postshock_coverage']['coverage'].get('missing_shock_hour',0)}.",
 '- All price features require source and receipt timestamps no later than the evaluated time. Completed bars only; no volume confirmation is claimed.',
 '- Replay covers unknown prior outcome waits, late winning-result release, loss streaks, loss boosts, daily target checks during waits, and daily sequence reset.',
 '- Delayed-entry regime rechecks were run separately: post-shock main results were unchanged. Persistent-lock and short-chop results differ, and are included in results.json.',
 '- Six automated checks cover delayed data, unknown results, late-win cushion release, target crossing during a wait, skipped-trade boost state, and indicator gap/causality handling.',
 '- Live retries/chase, partial fills, liquidity, rejected orders, exact broker polling and cash balance constraints are not replayed. Thus this is still not a faithful broker execution simulation.',
 '- Actual history has changing stakes/rules; modeled dollars use one fixed opening and current base/boost policy. Both are labeled separately.',
 '- All days were already inspected; the first 10/last 5 split is descriptive, not a genuinely untouched holdout. Candidates and threshold exploration are hindsight research.',
 '', '## Full candidate results','']
lines+=table(['Rule','Target','Net','Worst day','Max intraday DD','Target days'],[(names[r['rule']],f"{r['rate']:.0%}" if r['rate'] else 'No cap',money(r['net']),money(r['worst_day']),f"${r['max_intraday_dd']:.2f}",r['target_days']) for r in R['summary'] if r['period']=='all15'])
lines+=['', '## Decision supported by this study','',
 'The missing protection is admission control for a price range after a large move; a label or an isolated short-momentum check is insufficient. The evidence supports testing a persistent range state and post-shock detector prospectively, with frozen settings, on the all-signal path including waits/retries. It does not support raising the live target to 8% now. Five percent is the more resilient research candidate in these stress tests, not a newly proven live rule. Collect an untouched forward comparison before treating the apparent improvement as established.','',
 'Reproduction: run study.py, test_study.py with pytest, then emit_report.py. All production SQLite connections use mode=ro. inputs.json.gz freezes the signal, poll, BRTI and actual-event inputs used; CSV files expose per-day and per-trade calculations. Source hashes are in results.json.']
(P/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(P/'README.md')
