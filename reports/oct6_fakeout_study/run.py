"""Offline causal entry-filter study. Read-only databases; no live imports/settings writes."""
import bisect,csv,json,sqlite3,sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from btc15_signal.validation import contracts_for_budget,kalshi_fee_charged
NY=ZoneInfo('America/New_York'); OUT=Path(__file__).resolve().parent
start=int(datetime(2026,9,23,tzinfo=NY).timestamp()*1000)
end=int(datetime(2026,10,7,18,15,tzinfo=NY).timestamp()*1000)
c=sqlite3.connect('file:btc15.db?mode=ro',uri=True)
ref=sqlite3.connect('file:runtime/settlement_reference.db?mode=ro',uri=True)
brti=ref.execute('select ts_ms,received_ms,brti_value from brti_features where stale=0 and ts_ms between ? and ? order by ts_ms,received_ms',(start-3600000,end)).fetchall()
ts=[r[0] for r in brti]
def value(t):
 i=bisect.bisect_right(ts,t)-1
 while i>=0 and brti[i][0]>=t-60000:
  if brti[i][1]<=t:return brti[i][2]
  i-=1
 return None
rows=c.execute("""select a.window_open,a.created_at,o.side,o.our_ask,o.target,p.won,p.side
from strategy_alerts a join observations o on o.window_open=a.window_open and o.observed_ms=a.created_at
join predictions p on p.window_open=a.window_open
where a.strategy='primary' and a.window_open>=? and a.window_open+900000<=? order by a.window_open""",(start,end)).fetchall()
features=[]
for wo,at,side,ask,strike,won,pside in rows:
 if won is None or ask is None or not 0<ask<1 or (pside and pside!=side):continue
 # Completed minute snapshots; no future received data or partially formed candle.
 boundary=at//60000*60000
 prices=[value(boundary-k*60000) for k in range(31,-1,-1)]
 valid=all(p is not None for p in prices) and strike is not None
 efficiency=crossings=confirmed=None
 if valid:
  recent=prices[-16:];travel=sum(abs(b-a) for a,b in zip(recent,recent[1:]))
  efficiency=abs(recent[-1]-recent[0])/travel if travel else 0.
  signs=[1 if p>strike else -1 for p in recent if p!=strike]
  crossings=sum(a!=b for a,b in zip(signs,signs[1:]))
  # Boundary fixed before the two confirmation minutes; 2bps buffer.
  hi=max(prices[:-2])*1.0002;lo=min(prices[:-2])*.9998
  confirmed=all(p>hi for p in prices[-2:]) if side=='UP' else all(p<lo for p in prices[-2:])
 n=contracts_for_budget(25,ask);net=n*(int(won)-ask)-kalshi_fee_charged(ask,n)
 features.append(dict(window_open=wo,signal_ms=at,day=datetime.fromtimestamp(wo/1000,NY).strftime('%Y-%m-%d'),time=datetime.fromtimestamp(at/1000,NY).isoformat(),side=side,ask=ask,strike=strike,won=int(won),coverage=valid,efficiency15=efficiency,crossings15=crossings,breakout=confirmed,net=net))
assert len({r['window_open'] for r in features})==len(features)
rules={'baseline':lambda r:False,'low_progress':lambda r:r['efficiency15']<=.25,'strike_whipsaw':lambda r:r['crossings15']>=3,'both':lambda r:r['efficiency15']<=.25 and r['crossings15']>=3,'either_with_breakout_release':lambda r:(r['efficiency15']<=.25 or r['crossings15']>=3) and not r['breakout']}
results=[]
for name,fn in rules.items():
 for label,select in [('all15',lambda r:True),('before_oct6',lambda r:r['day']<'2026-10-06'),('oct6',lambda r:r['day']=='2026-10-06'),('oct6_chart_11_20_30',lambda r:r['day']=='2026-10-06' and '11:00'<=r['time'][11:16]<='20:30')]:
  subset=[r for r in features if select(r)];blocked=[r for r in subset if r['coverage'] and fn(r)];kept=[r for r in subset if not(r['coverage'] and fn(r))]
  results.append(dict(rule=name,period=label,signals=len(subset),missing=sum(not r['coverage'] for r in subset),blocked=len(blocked),blocked_wins=sum(r['won'] for r in blocked),blocked_losses=sum(1-r['won'] for r in blocked),net=round(sum(r['net'] for r in kept),2),change=round(-sum(r['net'] for r in blocked),2)))
# Actual lifecycle attribution: compare removed recorded outcomes, with historical sizes
# frozen. This is NOT a replay of loss boosts, subsequent fills or daily stops.
a=int(datetime(2026,10,6,tzinfo=NY).timestamp()*1000)
actual=c.execute("select t.window_open,t.created_ms,t.side,t.filled,t.fill_price,sum(e.amount) from allsignal_trades t join realised_events e on e.ticker=t.ticker where e.window_ms>=? and e.window_ms<? group by t.window_open",(a,a+86400000)).fetchall()
bywo={r['window_open']:r for r in features};attribution=[]
for name,fn in rules.items():
 matched=[(r,bywo[r[0]]) for r in actual if r[0] in bywo and bywo[r[0]]['signal_ms']<=r[1]]
 removed=[r for r,f in matched if f['coverage'] and fn(f)]
 attribution.append(dict(rule=name,matched=len(matched),actual_rows=len(actual),removed=len(removed),removed_losses=sum(r[5]<0 for r in removed),removed_wins=sum(r[5]>0 for r in removed),removed_net=round(sum(r[5] for r in removed),2),matched_actual_net=round(sum(r[5] for r,f in matched),2)))
with (OUT/'signals.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(features[0]));w.writeheader();w.writerows(features)
result=dict(start='2026-09-23',end='2026-10-07 18:15 ET',results=results,actual_oct6_attribution=attribution,limitations=['Fixed $25 stake, observed ask, settlement outcome and estimated fee: not live strategy replay.','No loss boosts, chase, early cashout, spread/slippage or fill uncertainty simulated.','Missing BRTI histories pass through, explicitly counted.','Actual attribution holds later historical sizes/outcomes fixed; not attainable counterfactual account profit.','Screenshot is another venue; image alone does not establish cause.','Thresholds chosen for this exploratory study; earlier days are a comparison, not an independent holdout.'])
(OUT/'results.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
