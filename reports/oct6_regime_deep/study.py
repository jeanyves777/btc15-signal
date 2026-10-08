"""Offline BTC regime/target study. All production databases opened mode=ro.

Counterfactual quote-fill replay, NOT a faithful execution simulator. The report
separately exposes observed lifecycle timing and assumed timing. No network calls.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged

NY = ZoneInfo("America/New_York")
MINUTE = 60000
START = int(datetime(2026, 9, 23, tzinfo=NY).timestamp() * 1000)
END = int(datetime(2026, 10, 7, 18, 15, tzinfo=NY).timestamp() * 1000)
CAPITAL = 995.93


def ro(path):
    c = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, NY).isoformat()


class Tape:
    def __init__(self):
        c = ro(ROOT / "btc15.db")
        c.execute("BEGIN")
        self.signals = [dict(r) for r in c.execute("""
            SELECT a.window_open wo, a.created_at at, o.side, o.our_ask ask,
                   o.target strike, o.distance_bps distance, p.won, o.ticker,
                   t.graded_ms, t.exited_ms, t.status, t.created_ms,
                   s.settled_ms, s.facts_synced_ms
            FROM strategy_alerts a JOIN observations o
              ON o.window_open=a.window_open AND o.observed_ms=a.created_at
            JOIN predictions p ON p.window_open=a.window_open
            LEFT JOIN allsignal_trades t ON t.window_open=a.window_open
            LEFT JOIN settlements s ON s.ticker=o.ticker
            WHERE a.strategy='primary' AND a.window_open>=?
              AND a.window_open+900000<=? AND p.won IS NOT NULL
              AND o.our_ask>0 AND o.our_ask<1 AND p.side=o.side
            ORDER BY a.window_open
        """, (START, END))]
        assert len(self.signals) == len({r['wo'] for r in self.signals})
        self.polls = defaultdict(list)
        for r in c.execute("""SELECT window_open, observed_ms at, side, yes_ask,
                no_ask, distance_bps distance, remaining_s FROM observations
                WHERE window_open>=? AND window_open+900000<=?
                ORDER BY observed_ms""", (START, END)):
            self.polls[r['window_open']].append(dict(r))
        self.actual = [dict(r) for r in c.execute("""SELECT t.window_open wo,
            t.created_ms at,t.side,t.fill_price,t.filled,t.fee,t.exited_ms,
            SUM(e.amount) net, MAX(e.realised_ms) realised_ms
            FROM allsignal_trades t JOIN realised_events e ON e.ticker=t.ticker
            WHERE t.window_open>=? AND t.window_open+900000<=?
            GROUP BY t.window_open ORDER BY t.created_ms""", (START, END))]
        c.close()
        ref = ro(ROOT / "runtime/settlement_reference.db")
        self.brti = [tuple(r) for r in ref.execute("""SELECT ts_ms,received_ms,brti_value
            FROM brti_features WHERE stale=0 AND ts_ms>=? AND ts_ms<=?
            ORDER BY ts_ms,received_ms""", (START-2*86400000, END))]
        ref.close()
        self.ts = [r[0] for r in self.brti]
        self.minute_values = {}
        ref = ro(ROOT / "runtime/settlement_reference.db")
        reconciled = {r['window_open_ms']: dict(r) for r in ref.execute(
            "SELECT window_open_ms,official_result,reconciled_ms FROM settlement_reconciliation "
            "WHERE window_open_ms>=? AND window_open_ms+900000<=?", (START, END))}
        ref.close()
        for s in self.signals:
            s['day'] = stamp(s['wo'])[:10]
            close = s['wo'] + 900000
            # Initial settlement-grade timestamp, only for hold-to-settlement rows.
            # An early cashout is NOT knowledge of the final market outcome.
            grade = s['graded_ms']
            reliable_grade = (s['status'] == 'filled' and not s['exited_ms']
                              and grade and grade >= close)
            official = reconciled.get(s['wo'])
            if official and official['official_result'] in ('yes','no'):
                assert bool(s['won']) == ((official['official_result']=='yes') == (s['side']=='UP'))
            if reliable_grade:
                s['known'] = grade
                s['timing_source'] = 'observed_grade'
            elif official and official['reconciled_ms']>=close:
                s['known'] = official['reconciled_ms']
                s['timing_source'] = 'shadow_official_arrival'
            else:
                s['known'] = close+60000
                s['timing_source'] = 'assumed_close_plus_60s'
            s.update(self.features(s))

    def value(self, at):
        if at in self.minute_values:
            return self.minute_values[at]
        i = bisect.bisect_right(self.ts, at) - 1
        while i >= 0 and self.brti[i][0] >= at-60000:
            ts, received, value = self.brti[i]
            if received <= at:
                self.minute_values[at] = value
                return value
            i -= 1
        self.minute_values[at] = None
        return None

    def features(self, s):
        at = s['at']//MINUTE*MINUTE
        p = [self.value(at-k*MINUTE) for k in range(60, -1, -1)]
        def efficiency(seq):
            if any(x is None for x in seq):
                return None
            travel = sum(abs(b-a) for a,b in zip(seq,seq[1:]))
            return abs(seq[-1]-seq[0])/travel if travel else 0.
        r = dict(er15=efficiency(p[-16:]), er60=efficiency(p), crossings=None,
                 trend15=None, trend60=None)
        if all(x is not None for x in p[-16:]):
            signs=[1 if x>s['strike'] else -1 for x in p[-16:] if x!=s['strike']]
            r['crossings']=sum(a!=b for a,b in zip(signs,signs[1:]))
        now = self.value(s['at'])
        for n in (15,60):
            old = self.value(s['at']-n*MINUTE)
            if now and old:
                r[f'trend{n}'] = 10000*(now/old-1)
        return r


def opposing(s, key='trend15', threshold=10):
    x = s.get(key)
    return x is not None and (x < -threshold if s['side']=='UP' else x > threshold)


def rule_blocks(s, name):
    er, cross = s['er15'], s['crossings']
    if name == 'baseline' or name == 'no_loss_boost':
        return False
    if name == 'chop_and_trend':
        return rule_blocks(s,'chop_0.25_3') or opposing(s)
    if name.startswith('chop_'):
        _, threshold, crossings = name.split('_')
        return er is not None and cross is not None and er<=float(threshold) and cross>=int(crossings)
    if name == 'hour_chop':
        return s['er60'] is not None and s['er60']<=.15
    if name == 'trend_always':
        return opposing(s)
    if name == 'ask_floor_70':
        return s['ask']<.70
    if name == 'ohlc_chop':
        return bool(s.get('regime_chop'))
    if name == 'ohlc_lock':
        return bool(s.get('locked')) and s.get('release_side')!=s['side']
    if name == 'postshock':
        return bool(s.get('postshock'))
    raise ValueError(name)


def known_result(trade, now):
    return bool(trade['won']) if trade['known']<=now else None


def boosted(trades, now):
    since = None
    for t in trades:
        since = 0 if known_result(t,now) is False else (None if since is None else since+1)
    return since is not None and since<2


def replay(tape, rule, target_rate, delay=60, slippage=0., capital=CAPITAL):
    daily=[];ledger=[]
    grouped=defaultdict(list)
    for s in tape.signals:
        grouped[s['day']].append(s)
    for day, signals in sorted(grouped.items()):
        trades=[];pnl=peak=dd=0.;hit=None;reasons=Counter()
        def flush(now):
            nonlocal pnl,peak,dd,hit
            for t in sorted(trades,key=lambda t:t['known']):
                if t['known']<=now and not t['booked']:
                    t['booked']=True;pnl+=t['net'];peak=max(peak,pnl);dd=max(dd,peak-pnl)
                    if target_rate and hit is None and pnl+1e-8>=capital*target_rate:
                        hit=t['known']
        for s in signals:
            now=s['at'];flush(now)
            reason='target' if hit else 'regime' if rule_blocks(s,rule) else None
            if not reason and len(trades)>=2 and all(known_result(t,now) is False for t in trades[-2:]) and opposing(s):
                reason='existing_trend_skip'
            price=s['ask'];entry=now
            if not reason and trades and known_result(trades[-1],now) is not True:
                found=False
                for poll in tape.polls[s['wo']]:
                    if poll['at']<now:continue
                    if poll['at']>=s['wo']+900000 or (poll['remaining_s'] is not None and poll['remaining_s']<120):break
                    flush(poll['at'])
                    if hit:
                        reason='target';break
                    cushion=poll['distance']
                    if cushion is None:continue
                    if poll['side']!=s['side']:cushion=-cushion
                    ask=poll['yes_ask'] if s['side']=='UP' else poll['no_ask']
                    need=0 if known_result(trades[-1],poll['at']) is True else 5
                    if ask and 0<ask<1 and (cushion>0 if need==0 else cushion>=need):
                        price=ask;entry=poll['at'];found=True;break
                if not found and not reason:reason='no_cushion'
            if reason:
                reasons[reason]+=1;continue
            flush(entry)
            if hit:
                reasons['target']+=1;continue
            stake=30 if rule!='no_loss_boost' and boosted(trades,entry) else 25
            price=min(.99,price+slippage)
            n=contracts_for_budget(stake,price)
            net=n*(s['won']-price)-kalshi_fee_charged(price,n)
            known=s['known'] if s['timing_source']!='assumed_close_plus_60s' else s['wo']+900000+delay*1000
            if delay != 60:
                known=max(known,s['wo']+900000+delay*1000)
            t=dict(day=day,rule=rule,rate=target_rate,wo=s['wo'],at=entry,side=s['side'],
                   won=s['won'],price=price,stake=stake,contracts=n,net=net,known=known,
                   timing_source=s['timing_source'],booked=False)
            assert known>=s['wo']+900000 and entry>=s['at']
            trades.append(t)
        flush(float('inf'))
        daily.append(dict(day=day,rule=rule,rate=target_rate,net=round(pnl,4),dd=round(dd,4),
                          trades=len(trades),wins=sum(t['won'] for t in trades),
                          hit=stamp(hit) if hit else None,skips=dict(reasons)))
        ledger.extend(trades)
    return daily,ledger


def aggregate(days):
    net=peak=dd=0
    for r in days:
        net+=r['net'];peak=max(peak,net);dd=max(dd,peak-net)
    return dict(net=round(net,2),worst_day=round(min(r['net'] for r in days),2),
                max_intraday_dd=round(max(r['dd'] for r in days),2),
                endofday_dd=round(dd,2),target_days=sum(bool(r['hit']) for r in days),
                losing_days=sum(r['net']<0 for r in days),trades=sum(r['trades'] for r in days))


def write_csv(path, rows):
    if not rows:return
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)


def attach_ohlc(tape):
    from ohlc_regime import build_states
    states=build_states(ROOT/'runtime/settlement_reference.db',START,END)
    # Event state machine at each completed minute. Lock boundaries freeze at
    # detection from the preceding valid 14 five-minute bars (70 minutes).
    times=[r['at_ms'] for r in states];locked=False;high=low=buffer=None
    previous_state=None;above=below=0;release_side=None;last_release=None
    events=[];byminute={}
    for at in range(START//MINUTE*MINUTE,END+1,MINUTE):
        idx=bisect.bisect_right(times,at)-1
        if idx<0:continue
        state=states[idx]
        # Evaluate a fresh chop diagnosis only once per completed 5-minute bar.
        if state['at_ms']!=previous_state:
            previous_state=state['at_ms']
            recent=states[max(0,idx-13):idx+1]
            if not locked and state.get('valid') and state.get('regime_chop') and len(recent)==14 and all(r.get('valid') for r in recent):
                locked=True;release_side=None
                high=max(r['high'] for r in recent);low=min(r['low'] for r in recent)
                buffer=.25*state['atr'];above=below=0
                events.append(dict(at=at,type='lock',high=high,low=low,buffer=buffer))
        v=tape.value(at)
        if locked:
            above=above+1 if v is not None and v>high+buffer else 0
            below=below+1 if v is not None and v<low-buffer else 0
            if above>=2 or below>=2:
                locked=False;release_side='UP' if above>=2 else 'DOWN';last_release=at
                events.append(dict(at=at,type='release',side=release_side,high=high,low=low))
        elif last_release and v is not None and low<=v<=high:
            # A failed breakout relocks the same frozen range immediately.
            locked=True;release_side=None;last_release=None;above=below=0
            events.append(dict(at=at,type='false_breakout',high=high,low=low))
        byminute[at]=dict(locked=locked,release_side=release_side,adx=state.get('adx'),
                         chop=state.get('chop'),regime_chop=state.get('regime_chop'),
                         ohlc_valid=state.get('valid'))
    for s in tape.signals:s.update(byminute.get(s['at']//MINUTE*MINUTE,{}))
    write_csv(OUT/'ohlc_states.csv',states)
    write_csv(OUT/'lock_events.csv',events)
    return events


def main():
    tape=Tape();events=attach_ohlc(tape)
    from postshock import add_postshock
    add_postshock(tape)
    rules=['baseline','chop_0.25_3','hour_chop','ohlc_chop','ohlc_lock',
           'trend_always','chop_and_trend','ask_floor_70','no_loss_boost','postshock']
    all_days=[];all_ledger=[];summary=[]
    for rule in rules:
        for rate in (.03,.05,.08,0):
            days,ledger=replay(tape,rule,rate);all_days.extend(days);all_ledger.extend(ledger)
            for period,sel in [('all15',lambda d:True),('early_10',lambda d:d<'2026-10-03'),
                               ('late_5',lambda d:d>='2026-10-03'),('oct6',lambda d:d=='2026-10-06')]:
                chosen=[r for r in days if sel(r['day'])]
                summary.append(dict(rule=rule,rate=rate,period=period,**aggregate(chosen)))
    # Nearby settings reported in full rather than selecting the single best.
    sensitivity=[]
    for er in (.20,.25,.30):
        for cross in (2,3,4):
            name=f'chop_{er}_{cross}'
            for rate in (.05,.08):
                days,_=replay(tape,name,rate)
                sensitivity.append(dict(rule=name,rate=rate,**aggregate(days)))
    execution_sensitivity=[]
    for rule in ('baseline','chop_0.25_3','hour_chop','ohlc_lock','ask_floor_70'):
        for rate in (.03,.05,.08):
            for delay,slip in ((300,0),(60,.01),(60,.02)):
                days,_=replay(tape,rule,rate,delay,slip)
                execution_sensitivity.append(dict(rule=rule,rate=rate,delay=delay,slippage=slip,**aggregate(days)))
    # Attribution on real trades only; does not claim alternative live execution.
    bywo={s['wo']:s for s in tape.signals};actual=[]
    for r in tape.actual:
        s=bywo.get(r['wo'])
        if not s or s['at']>r['at']:continue
        actual.append(dict(**r,day=s['day'],hour=int(stamp(r['at'])[11:13]),
                           **{f'blocked_{rule}':rule_blocks(s,rule) for rule in rules}))
    write_csv(OUT/'signals.csv',tape.signals);write_csv(OUT/'daily.csv',all_days)
    write_csv(OUT/'modeled_trades.csv',all_ledger);write_csv(OUT/'actual_attribution.csv',actual)
    result=dict(window=dict(start=stamp(START),end=stamp(END),signals=len(tape.signals)),
        assumptions=dict(capital_fixed=CAPITAL,base=25,boost=30,boost_trades=2,cushion_bps=5,
                         trend_skip_after_losses=2,trend_bps=10,fees='local fee formula',
                         cashout=False,missing_timing_delay_seconds=60),
        coverage=dict(timing=dict(Counter(s['timing_source'] for s in tape.signals)),
                      er15_missing=sum(s['er15'] is None for s in tape.signals),
                      er60_missing=sum(s['er60'] is None for s in tape.signals),
                      ohlc_missing=sum(not s.get('ohlc_valid') for s in tape.signals)),
        summary=summary,sensitivity=sensitivity,execution_sensitivity=execution_sensitivity,
        source_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (OUT/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'coverage':result['coverage'],'main':[r for r in summary if r['period']=='all15' and r['rate'] in (.03,.05,.08)]},indent=2))


if __name__=='__main__':main()
