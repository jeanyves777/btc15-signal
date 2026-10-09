"""Extend Claude's fixed-opening primary study to an explicit recorded window cutoff.
Read-only inputs. Counterfactual quote fills, NOT an execution backtest.
"""
import csv, json, sqlite3, hashlib
from pathlib import Path
from datetime import datetime
from collections import Counter
from zoneinfo import ZoneInfo
OUT=Path(__file__).resolve().parent
NY=ZoneInfo('America/New_York')
START=int(datetime(2026,9,23,tzinfo=NY).timestamp()*1000)
END=int(datetime(2026,10,7,18,15,tzinfo=NY).timestamp()*1000)
src=(OUT/'claude_trendfix_reference.py').read_text(encoding='utf-8')
head=src[:src.index('SIG = {')].replace('START = 1790135100000',f'START = {START}')
ns={'__name__':'recorded_study'}
exec(compile(head,'claude_reference_loader','exec'),ns)
from btc15_signal.config import Settings
s=Settings(_env_file=r'D:\Kalshi\btc15-signal\.env')
signals=[r for r in ns['signals'] if r['wo']+900000<=END]
assert len({r['wo'] for r in signals})==len(signals)
con=sqlite3.connect('file:D:/Kalshi/btc15-signal/btc15.db?mode=ro',uri=True);con.row_factory=sqlite3.Row
actual={r['window_open']:dict(r) for r in con.execute('select * from allsignal_trades where window_open>=? and window_open+900000<=?',(START,END))}
raw=list(con.execute("select a.window_open,o.our_ask,o.side,p.won,p.side pside from strategy_alerts a left join observations o on o.window_open=a.window_open and o.observed_ms=a.created_at left join predictions p on p.window_open=a.window_open where a.strategy='primary' and a.window_open>=? and a.window_open+900000<=?",(START,END)))
capital=995.93;target=capital*s.daily_profit_stop_rate
daily=[];ledger=[]
for day in sorted({r['day'] for r in signals}):
 pnl=low=0.;pending=streak=0;last=None;hit=None;wins=losses=0;reasons=Counter()
 for sig in (r for r in signals if r['day']==day):
  wo=sig['wo'];row={'day':day,'window_open':wo,'signal_ms':sig['at'],'side':sig['side'],'won':sig['won'],'signal_ask':sig['ask'],'trend_15m_bps':sig['tr'][15]}
  row['recorded_status']=actual.get(wo,{}).get('status','no dedicated lifecycle row')
  reason=None;ask=sig['ask'];entry=sig['at']
  if hit is not None:reason='daily_target'
  elif streak>=s.allsignal_trend_skip_after_losses and ns['against'](sig['side'],sig['tr'][15],s.allsignal_trend_skip_bps):reason='trend_skip'
  elif last is False:
   found=False
   for ms,side,ya,na,dist,remaining in ns['polls'][wo]:
    if ms<sig['at']:continue
    if ms>=wo+900000 or remaining is not None and remaining<s.allsignal_cushion_min_left_s:break
    price=ya if sig['side']=='UP' else na
    cushion=(dist or 0) if side==sig['side'] else -(dist or 0)
    if price is not None and 0<price<1 and cushion>=s.allsignal_after_loss_cushion_bps:
     ask=float(price);entry=ms;found=True;break
   if not found:reason='no_cushion'
  if reason:
   row['decision']=reason;reasons[reason]+=1;ledger.append(row);continue
  stake=s.allsignal_after_loss_stake if pending else s.allsignal_stake
  n=ns['contracts_for_budget'](stake,ask);fee=ns['kalshi_fee_charged'](ask,n)
  net=n*(sig['won']-ask)-fee;pnl+=net;low=min(low,pnl)
  last=bool(sig['won']);streak=0 if last else streak+1
  pending=max(0,pending-1) if last else s.allsignal_after_loss_trades
  wins+=int(last);losses+=int(not last)
  if pnl+1e-8>=target:hit=datetime.fromtimestamp((wo+900000)/1000,NY).strftime('%H:%M')
  row.update(decision='taken',entry_ms=entry,entry_ask=ask,stake=stake,contracts=n,fee=fee,net=net,cumulative_day=pnl)
  ledger.append(row)
 daily.append(dict(day=day,signals=sum(r['day']==day for r in signals),trades=wins+losses,wins=wins,losses=losses,net=round(pnl,4),low=round(low,4),target_hit=hit,skips=dict(reasons)))
columns=list(dict.fromkeys(k for r in ledger for k in r))
with (OUT/'trades.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(ledger)
summary={'start':'2026-09-23 00:00 ET','last_close':'2026-10-07 18:15 ET','capital_fixed_per_day':capital,'target':target,
 'signal_rows':len(raw),'matched_settled_signals':len(signals),'unmatched_or_unsettled':len(raw)-len(signals),
 'source_wins':sum(r['won'] for r in signals),'daily':daily,'total_net':round(sum(r['net'] for r in daily),2),
 'target_days':sum(r['target_hit'] is not None for r in daily),'losing_days':sum(r['net']<0 for r in daily),
 'modeled_trades':sum(r['trades'] for r in daily),'modeled_wins':sum(r['wins'] for r in daily),
 'recorded_lifecycle_statuses':dict(Counter(r['status'] for r in actual.values())),
 'settings':{k:getattr(s,k) for k in ['allsignal_stake','allsignal_after_loss_stake','allsignal_after_loss_trades','daily_profit_stop_rate','allsignal_after_loss_cushion_bps','allsignal_cushion_min_left_s','allsignal_trend_skip_after_losses','allsignal_trend_skip_bps','cash_out_enabled']},
 'reference_script_sha256':hashlib.sha256(src.encode()).hexdigest(),
 'limitations':['Real recorded signals, polls and outcomes; hypothetical fills at observed asks.','No order-book execution, retry or chase fill simulation.','Fixed 995.93 daily opening, no compounding or deposit reconstruction.','Previous outcome assumed known before next signal, as in original Claude study; delayed result availability not reconstructed.','Current-rule counterfactual, not historical account profit.']}
(OUT/'summary.json').write_text(json.dumps(summary,indent=2))
print(json.dumps(summary,indent=2))
