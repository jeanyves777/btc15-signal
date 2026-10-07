"""Match actual signal -> broker fill -> recorded exit/outcome. Read-only sources."""
import sqlite3, json, csv, bisect, math
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[2]; OUT=Path(__file__).resolve().parent
NY=ZoneInfo('America/New_York')
def db(p):
 c=sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;return c
c=db(ROOT/'btc15.db');h=db(ROOT/'runtime/hourly.db')
def fee(p,n):return math.ceil(round(.07*n*p*(1-p)*10000,6))/10000
chains=defaultdict(list)
for r in h.execute('select chain_id,fetched_ms,close_ms from hourly_chains order by fetched_ms'):
 chains[r['close_ms']].append((r['fetched_ms'],r['chain_id']))
times={k:[r[0] for r in v] for k,v in chains.items()}
source=c.execute('''select a.*,s.created_at signal_ms,o.target,p.won settlement_won,p.final_price,
 (select min(filled_ms) from fills f where f.order_id=a.order_id and action='buy') first_fill_ms,
 (select max(filled_ms) from fills f where f.order_id=a.order_id and action='buy') last_fill_ms
 from allsignal_trades a join strategy_alerts s on s.window_open=a.window_open and s.strategy='primary'
 join observations o on o.window_open=s.window_open and o.observed_ms=s.created_at and o.side=a.side
 join predictions p on p.window_open=a.window_open and p.side=a.side
 where a.filled>0 and a.pnl is not null and p.won is not null order by a.window_open''').fetchall()
summary={}
for mode in ['quarter_close','actual_exit']:
 rows=[];drop=Counter()
 for s in source:
  entry=s['first_fill_ms'];close=s['window_open']+900000
  end=s['exited_ms'] if mode=='actual_exit' and s['exited_ms'] else close
  expiry=((close+3599999)//3600000)*3600000;ts=times.get(expiry,[])
  if entry is None:drop['no_broker_fill_timestamp']+=1;continue
  i=bisect.bisect_right(ts,entry)-1;j=bisect.bisect_left(ts,end)-1
  if i<0 or entry-ts[i]>90000:drop['entry_gap']+=1;continue
  if j<=i or end-ts[j]>90000:drop['exit_gap']+=1;continue
  chain=chains[expiry][i][1]
  ladder=h.execute('select * from hourly_strikes where chain_id=? and fetched_ms=?',(chain,ts[i])).fetchall()
  if not ladder or s['target'] is None:drop['no_strike']+=1;continue
  r=min(ladder,key=lambda x:(abs(x['strike']-s['target']),x['strike']))
  x=h.execute('select * from hourly_strikes where chain_id=? and fetched_ms=? and ticker=?',(chain,ts[j],r['ticker'])).fetchone()
  if x is None:drop['missing_exit_strike']+=1;continue
  side='yes' if s['side']=='UP' else 'no';ask=r[side+'_ask'];bid=x[side+'_bid']
  if ask is None or bid is None or not 0<ask<1 or not 0<=bid<=1 or r[side+'_bid']>ask or bid>x[side+'_ask']:
   drop['invalid_quote']+=1;continue
  # Same actual filled entry principal, not an invented $25 stake.
  capital=s['filled']*s['fill_price'];n=math.floor(capital/ask)
  if n<1:drop['budget_below_one_contract']+=1;continue
  pnl=n*(bid-ask)-fee(ask,n)-fee(bid,n)
  row=dict(window_open=s['window_open'],day=datetime.fromtimestamp(s['window_open']/1000,NY).strftime('%Y-%m-%d'),
   btc_ticker=s['ticker'],side=s['side'],signal_ms=s['signal_ms'],first_fill_ms=entry,last_fill_ms=s['last_fill_ms'],
   btc_fill_price=s['fill_price'],btc_filled=s['filled'],btc_entry_principal=capital,btc_fee_recorded=s['fee'],
   btc_exit_ms=s['exited_ms'],btc_exit_price=s['exit_price'],btc_exit_count=s['exit_count'],btc_exit_fee=s['exit_fee'],
   btc_recorded_net_pnl=s['pnl'],btc_settlement_won=s['settlement_won'],btc_final_price=s['final_price'],
   target=s['target'],hourly_ticker=r['ticker'],hourly_strike=r['strike'],hourly_entry_ms=ts[i],hourly_exit_ms=ts[j],
   hourly_ask=ask,hourly_bid=bid,hourly_count=n,hourly_quote_pnl=pnl,requested_exit_ms=end,
   entry_age_s=(entry-ts[i])/1000,exit_early_s=(end-ts[j])/1000)
  assert ts[i]<ts[j]<end<=expiry
  rows.append(row)
 with (OUT/('lifecycle_'+mode+'.csv')).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
 daily=defaultdict(lambda:[0,0,0])
 for r in rows:
  d=daily[r['day']];d[0]+=r['btc_recorded_net_pnl'];d[1]+=r['hourly_quote_pnl'];d[2]+=1
 summary[mode]=dict(source_n=len(source),matched=len(rows),excluded=dict(drop),
  recorded_btc_net=round(sum(r['btc_recorded_net_pnl'] for r in rows),2),
  hourly_quote_net=round(sum(r['hourly_quote_pnl'] for r in rows),2),
  entry_principal=round(sum(r['btc_entry_principal'] for r in rows),2),
  early_exits=sum(bool(r['btc_exit_ms']) for r in rows),days={k:[round(v[0],2),round(v[1],2),v[2]] for k,v in daily.items()})
summary['source_recorded_net']=round(sum(r['pnl'] for r in source),2)
(OUT/'lifecycle_results.json').write_text(json.dumps(summary,indent=2))
print(json.dumps(summary,indent=2))
