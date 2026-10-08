"""Lower-target prefixes of the frozen 15-day signal study, plus actual Oct 6 events.
Lowering only the stop cannot change the trade prefix before its first crossing.
No live settings or database writes.
"""
import csv,json,sqlite3
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
P=Path(__file__).resolve().parent;NY=ZoneInfo('America/New_York')
baseline=json.loads((P/'summary.json').read_text())
trades=defaultdict(list)
with (P/'trades.csv').open(newline='') as f:
 for r in csv.DictReader(f):
  if r['decision']=='taken':trades[r['day']].append(r)
rates=[.02,.03,.04,.05,.06,.07,.08]
allresults=[];daily=[]
for rate in rates:
 total=0.;hits=losing=0;worst=0.;n=0
 for day,rows in sorted(trades.items()):
  pnl=0.;hit=None;count=0;low=0.
  for r in rows:
   pnl+=float(r['net']);count+=1;low=min(low,pnl)
   if pnl+1e-8>=rate*baseline['capital_fixed_per_day']:
    hit=datetime.fromtimestamp((int(r['window_open'])+900000)/1000,NY).strftime('%H:%M');break
  total+=pnl;hits+=int(hit is not None);losing+=int(pnl<0);worst=min(worst,pnl);n+=count
  daily.append(dict(rate=rate,day=day,net=round(pnl,4),hit=hit,trades=count,low=round(low,4)))
 oct6=next(r for r in daily if r['rate']==rate and r['day']=='10-06')
 allresults.append(dict(rate=rate,target=round(rate*baseline['capital_fixed_per_day'],2),total=round(total,2),delta_vs_8=round(total-baseline['total_net'],2),hit_days=hits,losing_days=losing,worst_day=round(worst,2),trades=n,oct6=oct6))
assert abs(allresults[-1]['total']-baseline['total_net'])<.011
c=sqlite3.connect('file:D:/Kalshi/btc15-signal/btc15.db?mode=ro',uri=True)
a=int(datetime(2026,10,6,tzinfo=NY).timestamp()*1000);b=a+86400000
events=c.execute("select ticker,realised_ms,amount,recorded_ms from realised_events where window_ms>=? and window_ms<? and ticker like 'KXBTC15M-%' order by realised_ms,event_id",(a,b)).fetchall()
g=sqlite3.connect('file:D:/Kalshi/btc15-signal/runtime/daily_profit.db?mode=ro',uri=True)
opening,recorded_final=g.execute("select opening,pnl from profit_days where account='primary' and day='2026-10-06'").fetchone()
assert abs(sum(r[2] for r in events)-recorded_final)<1e-6
live=[]
for rate in rates:
 pnl=0.;hit=None;count=0;last=None;peak=0.
 for ticker,ms,amount,recorded in events:
  pnl+=amount;count+=1;peak=max(peak,pnl)
  if pnl+1e-8>=opening*rate:
   hit=datetime.fromtimestamp(ms/1000,NY).isoformat();last=ms;break
 overlap=[]
 if last:
  overlap=[r[0] for r in c.execute("select ticker from allsignal_trades where status='filled' and created_ms<? and window_open+900000>? and window_open>=?",(last,last,a))]
 live.append(dict(rate=rate,target=round(opening*rate,2),net_if_stopped=round(pnl,2),hit_time=hit,events=count,improvement=round(pnl-recorded_final,2),already_open_at_hit=overlap))
result={'model_capital':baseline['capital_fixed_per_day'],'modeled':allresults,'daily':daily,'oct6_actual_opening':opening,'oct6_actual_final':recorded_final,'oct6_recorded_event_count':len(events),'oct6_recorded':live}
(P/'target_sweep.json').write_text(json.dumps(result,indent=2))
with (P/'target_sweep_daily.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(daily[0]));w.writeheader();w.writerows(daily)
lines=['# Lower daily-profit targets: same 15-day record','',
'Only the daily stop threshold changes. Fixed $995.93 opening, same recorded signals/polls/outcomes and $25/$30 stakes as the preceding study. Each lower-target run is an exact prefix of the saved 8% run: no missing after-stop trades are needed to evaluate a LOWER stop. Fees already included. Counterfactual fills remain assumed at observed asks; this is not actual account profit.','',
'| Target | Dollar target | 15-day modeled net | Change vs 8% | Target days | Losing days | Oct 6 net |',
'|---|---:|---:|---:|---:|---:|---:|']
for r in allresults:lines.append(f"| {r['rate']:.0%} | ${r['target']:.2f} | ${r['total']:+.2f} | ${r['delta_vs_8']:+.2f} | {r['hit_days']}/15 | {r['losing_days']} | ${r['oct6']['net']:+.2f} |")
lines+=['','## Actual October 6 broker-derived event sequence','',f'Opening ${opening:.2f}; {len(events)} events; sum ${recorded_final:.2f}, matching the stored daily-profit record. These are retrospective prefixes of actual net events, not quote fills. Event timing uses realised_ms; exact guard polling/notice delay is not replayed. Event amounts may reflect later broker reconciliation. No fee subtraction was added.','',
'| Target | Dollar target | Recorded P&L at crossing or final | First crossing ET |','|---|---:|---:|---|']
for r in live:lines.append(f"| {r['rate']:.0%} | ${r['target']:.2f} | ${r['net_if_stopped']:+.2f} | {r['hit_time'] or 'Never reached'} |")
lines+=['','Already-open positions at each crossing are listed in target_sweep.json; a daily stop prevents new entries, not exits.','',
'This comparison selects thresholds on the same historical sample, so a better result here is not independent evidence of future superiority. No live change or service restart was made.']
(P/'target_sweep.md').write_text('\n'.join(lines),encoding='utf-8')
print(json.dumps({'modeled':allresults,'actual_oct6':live},indent=2))
