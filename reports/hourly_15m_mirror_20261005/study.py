"""Read-only archive study; writes only adjacent research outputs. No trading imports.
Nearest hourly strike at entry, same direction, exit just before 15m close.
Quotes are indicative, not order-book fills. No tuned strike or outcome selection.
"""
import bisect, csv, json, math, sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
NY = ZoneInfo('America/New_York')
START = int(datetime(2026, 9, 24, tzinfo=NY).timestamp()*1000)
def connect(path):
    c = sqlite3.connect('file:'+str(path)+'?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    return c
def fee(p,n):
    return math.ceil(round(.07*n*p*(1-p)*10000,6))/10000
def profit(a,b):
    n = math.floor(25/a)
    return n*(b-a)-fee(a,n)-fee(b,n), n*a+fee(a,n)
live, hourly = connect(ROOT/'btc15.db'), connect(ROOT/'runtime/hourly.db')
chains = defaultdict(list)
for r in hourly.execute('select chain_id,fetched_ms,close_ms from hourly_chains where fetched_ms>=? order by fetched_ms',(START,)):
    chains[r['close_ms']].append((r['fetched_ms'],r['chain_id']))
times = {end:[x[0] for x in rows] for end,rows in chains.items()}
signals = [dict(r) for r in live.execute('''select a.window_open,a.created_at as entered,o.target,o.side,o.our_ask as ask,p.won
 from strategy_alerts a join observations o on o.window_open=a.window_open and o.observed_ms=a.created_at
 join predictions p on p.window_open=a.window_open and p.side=o.side
 where a.strategy='primary' and a.window_open>=? and p.won is not null order by a.window_open''',(START,))]
trades = [dict(r) for r in live.execute('''select a.window_open,coalesce(a.attempt_ms,a.created_ms) as entered,p.target,a.side,
 coalesce(a.fill_price,a.ask) as ask,p.won,a.pnl as actual_pnl,a.stake
 from allsignal_trades a join predictions p on p.window_open=a.window_open and p.side=a.side
 where a.window_open>=? and a.filled>0 and p.won is not null order by a.window_open''',(START,))]
cache={}
def ladder(chain,ts):
    key=(chain,ts)
    if key not in cache:
        cache[key]=[dict(r) for r in hourly.execute('select * from hourly_strikes where chain_id=? and fetched_ms=?',key)]
    return cache[key]
def summarize(rows):
    hp=sum(r['hourly_pnl'] for r in rows);bp=sum(r['btc_pnl'] for r in rows)
    hc=sum(r['hourly_cost'] for r in rows);bc=sum(r['btc_cost'] for r in rows)
    daily=defaultdict(lambda:[0,0,0])
    for r in rows:
        d=daily[r['day']];d[0]+=r['hourly_pnl'];d[1]+=r['btc_pnl'];d[2]+=1
    return dict(n=len(rows),hourly_pnl=round(hp,2),btc_pnl=round(bp,2),difference=round(hp-bp,2),
        hourly_return_pct=round(100*hp/hc,2) if hc else None,btc_return_pct=round(100*bp/bc,2) if bc else None,
        hourly_profitable=sum(r['hourly_pnl']>0 for r in rows),btc_profitable=sum(r['btc_pnl']>0 for r in rows),
        mean_strike_gap=round(sum(abs(r['strike_gap']) for r in rows)/len(rows),2) if rows else None,
        hourly_after_1c_each_leg=round(sum(r['hourly_stressed'] for r in rows),2),
        days={k:[round(v[0],2),round(v[1],2),v[2]] for k,v in sorted(daily.items())})
results={}
for cohort,source in [('actual_taken',trades),('all_signals',signals)]:
 for mode in ['asof','next_poll']:
    rows=[];dropped=Counter()
    for s in source:
        wo=s['window_open']; close=wo+900000; expiry=((close+3599999)//3600000)*3600000
        ts=times.get(expiry,[]); ent=s['entered']
        i=bisect.bisect_right(ts,ent)-1
        if i<0 or ent-ts[i]>90000: dropped['no_entry_snapshot_within_90s']+=1;continue
        chain=chains[expiry][i][1]; opts=ladder(chain,ts[i])
        if not opts or s['target'] is None: dropped['no_strike_or_target']+=1;continue
        rung=min(opts,key=lambda r:(abs(r['strike']-s['target']),r['strike']))
        # Strike fixed using information available at the signal, even for delayed execution.
        if mode=='next_poll':
            i=bisect.bisect_left(ts,ent)
            if i==len(ts) or ts[i]-ent>90000 or ts[i]>=close: dropped['no_next_entry']+=1;continue
            found=next((r for r in ladder(chain,ts[i]) if r['ticker']==rung['ticker']),None)
            if found is None:dropped['entry_rung_missing']+=1;continue
            rung=found
        j=bisect.bisect_left(ts,close)-1
        if j<=i or close-ts[j]>90000:dropped['no_preclose_exit_within_90s']+=1;continue
        exitrow=next((r for r in ladder(chain,ts[j]) if r['ticker']==rung['ticker']),None)
        if exitrow is None:dropped['exit_rung_missing']+=1;continue
        side='yes' if s['side']=='UP' else 'no'
        ask=rung[side+'_ask'];bid=exitrow[side+'_bid']
        if ask is None or bid is None or not 0<ask<1 or not 0<=bid<=1 or not s['ask'] or not 0<s['ask']<1:
            dropped['invalid_price']+=1;continue
        if rung[side+'_bid']>ask or bid>exitrow[side+'_ask']:
            dropped['crossed_quote']+=1;continue
        assert expiry-3600000 <= wo < close <= expiry
        assert ts[i] < ts[j] < close
        hp,hc=profit(ask,bid);n=math.floor(25/s['ask']);bc=n*s['ask']+fee(s['ask'],n)
        bp=n*(s['won']-s['ask'])-fee(s['ask'],n)
        stressed,_=profit(min(.9999,ask+.01),max(0,bid-.01))
        rows.append(dict(day=datetime.fromtimestamp(wo/1000,NY).strftime('%Y-%m-%d'),window_open=wo,
            quarter=(wo//900000)%4+1,side=s['side'],target=s['target'],strike=rung['strike'],strike_gap=rung['strike']-s['target'],
            hourly_ticker=rung['ticker'],entry_quote_ms=ts[i],exit_quote_ms=ts[j],exit_early_s=(close-ts[j])/1000,
            hourly_ask=ask,hourly_bid=bid,hourly_pnl=hp,hourly_cost=hc,btc_pnl=bp,btc_cost=bc,
            hourly_stressed=stressed,btc_won=s['won']))
    key=cohort+'_'+mode
    results[key]={'source_n':len(source),'excluded':dict(dropped),'overall':summarize(rows),
        'quarter':{str(q):summarize([r for r in rows if r['quarter']==q]) for q in range(1,5)},
        'first_half':summarize(rows[:len(rows)//2]),'second_half':summarize(rows[len(rows)//2:])}
    if rows:
        with (OUT/(key+'.csv')).open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
(OUT/'results.json').write_text(json.dumps(results,indent=2))
for key,r in results.items():
    print(key,json.dumps({k:v for k,v in r.items() if k not in ['quarter','first_half','second_half']}))
    print('quarters',[(q,v['n'],v['hourly_pnl'],v['btc_pnl']) for q,v in r['quarter'].items()])
