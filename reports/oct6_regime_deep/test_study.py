import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from study import Tape, boosted, known_result, replay
from ohlc_regime import self_test


def signal(wo, at, won, known, ask=.7):
    return dict(wo=wo,at=at,won=won,known=known,ask=ask,day='2026-10-06',
                timing_source='observed_grade',side='UP',trend15=0,er15=1,crossings=0)


def test_received_time_blocks_future_reference():
    tape=object.__new__(Tape)
    tape.brti=[(10000,11000,100.),(20000,25000,110.)]
    tape.ts=[10000,20000];tape.minute_values={}
    assert tape.value(21000)==100.
    assert tape.value(25000)==110.
    assert tape.value(90000) is None


def test_unknown_outcome_is_not_loss_boost():
    trade=dict(known=1000,won=0)
    assert known_result(trade,999) is None
    assert not boosted([trade],999)
    assert boosted([trade],1000)


def test_late_winner_releases_cushion_above_zero():
    a=signal(0,240000,1,1200000)
    b=signal(900000,1140000,1,1860000)
    tape=SimpleNamespace(signals=[a,b],polls={900000:[
        dict(at=1140000,remaining_s=660,side='UP',distance=2,yes_ask=.7,no_ask=.3),
        dict(at=1200001,remaining_s=599,side='UP',distance=2,yes_ask=.72,no_ask=.28)]})
    _,trades=replay(tape,'baseline',0)
    assert trades[1]['at']==1200001
    assert trades[1]['price']==.72
    assert trades[1]['stake']==25


def test_target_during_wait_prevents_entry():
    a=signal(0,240000,1,1200000)
    b=signal(900000,1140000,1,1860000)
    tape=SimpleNamespace(signals=[a,b],polls={900000:[
        dict(at=1200001,remaining_s=599,side='UP',distance=6,yes_ask=.8,no_ask=.2)]})
    days,trades=replay(tape,'baseline',.03,capital=100)
    assert len(trades)==1
    assert days[0]['hit']
    assert days[0]['skips']['target']==1


def test_regime_skip_does_not_reset_taken_loss_sequence():
    a=signal(0,240000,0,960000)
    b=signal(900000,1140000,1,1860000);b['er15']=.1;b['crossings']=4
    d=signal(1800000,2040000,1,2760000)
    tape=SimpleNamespace(signals=[a,b,d],polls={1800000:[
        dict(at=2040000,remaining_s=660,side='UP',distance=6,yes_ask=.7,no_ask=.3)]})
    _,trades=replay(tape,'chop_0.25_3',0)
    assert len(trades)==2 and trades[1]['stake']==30


def test_indicator_causality_and_gap_handling():
    self_test()
