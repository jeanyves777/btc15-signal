"""Operator 2026-10-08: strategy skips are shadow; live waits for either side >=85c."""
import asyncio
import json

import pytest

from btc15_signal import main, messages
from btc15_signal.execution import ExecutionResult
from test_allsignal_ohlc_lock import W, X, TICKER, setup, contract, snap, alert, poll, row, events


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(main.asyncio, 'sleep', instant)
    main.ALLSIGNAL_WAIT.clear()
    main.ALLSIGNAL_WAIT_NOTE.clear()
    main.LOCK_WAIT.clear()
    yield
    main.ALLSIGNAL_WAIT.clear()
    main.LOCK_WAIT.clear()


def configured(tmp_path, **kw):
    return setup(tmp_path, allsignal_skip_wait_min_ask=.85,
                 allsignal_ohlc_lock_wait=False, **kw)


def trigger_loss(monkeypatch):
    monkeypatch.setattr(main, 'allsignal_after_loss', lambda *_: True)


def test_trend_skip_is_shadow_and_live_waits(tmp_path, monkeypatch):
    store, s, client = configured(tmp_path)
    monkeypatch.setattr(main, 'allsignal_trend_skip', lambda *_: 'two losses against trend')
    alert(store,s,client,.70)
    assert not client.orders and row(store) is None
    assert W in main.LOCK_WAIT
    assert any(e['event']=='strategy_skip_shadow' for e in events(s))
    poll(store,s,client,.85,X+10000,650)
    assert client.orders[0][0]=='UP' and row(store)[0]=='filled'


def test_after_loss_distance_does_not_release_below_85(tmp_path,monkeypatch):
    store,s,client=configured(tmp_path)
    trigger_loss(monkeypatch)
    alert(store,s,client,.70)  # snapshot already more than the old 5bps cushion
    poll(store,s,client,.84,X+10000,650)
    assert not client.orders and W in main.LOCK_WAIT
    poll(store,s,client,.85,X+20000,640)
    assert client.orders[0][0]=='UP'


def test_opposite_at_85_enters_even_after_loss(tmp_path,monkeypatch):
    store,s,client=configured(tmp_path)
    trigger_loss(monkeypatch)
    alert(store,s,client,.70)
    poll(store,s,client,.14,X+10000,650,down=.85)
    assert client.orders[0][0]=='DOWN'
    assert store.allsignal_open(W)['side']=='DOWN'
    assert not main.ALLSIGNAL_WAIT
    assert 'FLIPPED' in main._price_confirmation(store,W)['note']


def test_opposite_already_confirmed_at_alert_enters_now(tmp_path,monkeypatch):
    store,s,client=configured(tmp_path)
    trigger_loss(monkeypatch)
    alert(store,s,client,.14)
    assert client.orders[0][0]=='DOWN'


def test_wait_survives_restart_and_does_not_double_enter(tmp_path,monkeypatch):
    store,s,client=configured(tmp_path)
    trigger_loss(monkeypatch)
    alert(store,s,client,.70)
    main.LOCK_WAIT.clear()
    poll(store,s,client,.14,X+10000,650,down=.86)
    poll(store,s,client,.14,X+20000,640,down=.87)
    assert len(client.orders)==1 and client.orders[0][0]=='DOWN'
    assert not store.get_setting_text(main.PRICE_WAIT_KEY)


def test_expiring_old_wait_does_not_erase_new_window_wait(tmp_path):
    store,s,client=configured(tmp_path)
    current=W+900000
    main._begin_price_wait(store,s,W,TICKER,'UP',.70,.85,'old',X)
    main._begin_price_wait(store,s,current,TICKER,'UP',.70,.85,'new',X+900000)
    main.allsignal_lock_poll(store,s,client,contract(.70),snap(),current,660,X+900000)
    assert row(store)[0]=='price_expired'
    assert json.loads(store.get_setting_text(main.PRICE_WAIT_KEY))['opened']==current


def test_cutoff_cannot_be_bypassed_by_late_85_quote(tmp_path,monkeypatch):
    store,s,client=configured(tmp_path)
    trigger_loss(monkeypatch)
    alert(store,s,client,.70)
    poll(store,s,client,.87,W+781000,119)
    assert not client.orders and row(store)[0]=='price_expired'
    assert store.allsignal_unannounced_misses(W)[0]['status']=='price_expired'
    text=messages.allsignal_missed_message(asset='BTC',window_open=W,side='UP',
        signal_ask=.70,limit=0,confirmation_expired=True,reason=row(store)[1])
    assert 'PRICE CONFIRMATION EXPIRED' in text
    assert 'SKIPPED' not in text and 'limit 0' not in text


def test_ambiguous_or_invalid_asks_do_not_confirm(tmp_path,monkeypatch):
    store,s,client=configured(tmp_path)
    trigger_loss(monkeypatch)
    alert(store,s,client,.70)
    poll(store,s,client,.87,X+10000,650,down=.88)
    poll(store,s,client,1.0,X+20000,640,down=.01)
    poll(store,s,client,float('nan'),X+30000,630,down=.01)
    assert not client.orders and W in main.LOCK_WAIT


def test_every_signal_waits_for_price_confirmation(tmp_path):
    store,s,client=configured(tmp_path)
    alert(store,s,client,.70)
    assert not client.orders and W in main.LOCK_WAIT
    poll(store,s,client,.85,X+10000,650)
    assert client.orders[0][0]=='UP'


def test_universal_rule_can_flip_without_a_loss_or_skip(tmp_path):
    store,s,client=configured(tmp_path)
    alert(store,s,client,.70)
    poll(store,s,client,.14,X+10000,650,down=.85)
    assert client.orders[0][0]=='DOWN'
    assert 'FLIPPED' in main._price_confirmation(store,W)['note']


def test_legacy_persisted_cushion_converts_to_price_wait(tmp_path):
    store,s,client=configured(tmp_path)
    main._start_wait(store,W,'UP',.70,TICKER,X,2)
    main.ALLSIGNAL_WAIT.clear()
    async def run():
        main.allsignal_cushion_poll(store,s,client,contract(.84),snap(20),W,600,X+60000)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(run())
    assert not client.orders and W in main.LOCK_WAIT and not main.ALLSIGNAL_WAIT


def test_daily_pause_result_is_still_honored(tmp_path,monkeypatch):
    store,s,client=configured(tmp_path)
    trigger_loss(monkeypatch)
    async def paused(*_args,**_kwargs):
        return ExecutionResult('paused',0,None,None,'daily profit target reached')
    client.execute_with_take_profit=paused
    alert(store,s,client,.87)
    assert not client.orders and row(store)[0]=='paused'
    poll(store,s,client,.88,X+10000,650)
    assert row(store)[0]=='paused'


def test_retry_must_still_meet_confirmation_floor(tmp_path,monkeypatch):
    store,s,client=configured(tmp_path)
    store.allsignal_claim(W,TICKER,'DOWN',.86,29,.88,X,stake=25)
    store.allsignal_finish(W,'unfilled',order_id='ioc-miss')
    main._remember_price_confirmation(store,W,.85,'flipped at 86c',X)
    trigger_loss(monkeypatch)
    sent=[]
    async def order(*args):sent.append(args)
    monkeypatch.setattr(main,'_allsignal_order',order)
    async def run():
        main.allsignal_retry_poll(store,s,client,contract(.17,.84),snap(20),W,550,X+70000)
        assert not sent and not main.ALLSIGNAL_TASKS
        missed=store._dicts('SELECT * FROM allsignal_trades WHERE window_open=?',(W,))[0]
        assert missed['order_id']=='ioc-miss'
        assert 'no qualifying retry' in main._miss_reason(missed)
        main.allsignal_retry_poll(store,s,client,contract(.12,.87),snap(20),W,540,X+80000)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(run())
    assert len(sent)==1 and sent[0][4]=='DOWN'


def test_flipped_copy_reads_opposite_outcome_for_loss_rules():
    copied=dict(won=None,market_result=None,status='copied',p_won=1,p_side='UP',side='DOWN')
    assert main._taken_lost(copied) is True
    copied['p_won']=0
    assert main._taken_lost(copied) is False


def test_primary_and_all_mirrors_receive_confirmed_opposite_side(tmp_path,monkeypatch):
    from test_mirror import build, drain
    from btc15_signal.mirror import MirrorTarget
    from unittest.mock import AsyncMock
    store,s,_=configured(tmp_path)
    trigger_loss(monkeypatch)
    async def run():
        wrapper,primary,mirrors=build(monkeypatch,[
            MirrorTarget(f'm{i}',f'key{i}',f'path{i}',allsignal_budget=2)
            for i in (1,2,3)])
        primary.fill_detail=AsyncMock(return_value=None)
        main.allsignal_on_alert(store,s,wrapper,contract(.70),'UP',.70,snap(),W,X)
        main.allsignal_lock_poll(store,s,wrapper,contract(.14,.85),snap(),W,650,X+10000)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
        await drain(wrapper)
        assert primary.calls[0][2]=='DOWN'
        assert all(m.calls[0][2]=='DOWN' for m in mirrors)
    asyncio.run(run())


def test_flipped_copy_telegram_result_uses_executed_side(tmp_path,monkeypatch):
    store,s,_=configured(tmp_path)
    store.allsignal_claim(W,TICKER,'DOWN',.85,29,.87,X)
    store.allsignal_finish(W,'copied')
    store.db.execute('INSERT INTO predictions(window_open,created_at,target,entry_price,side,won) '
                     'VALUES(?,?,?,?,?,?)',(W,X,100000,.70,'UP',1))
    store.db.commit()
    sent=[]
    async def announce(*args,**kw):sent.append(kw['won'])
    monkeypatch.setattr(main,'announce_allsignal_copy',announce)
    asyncio.run(main.report_allsignal_results(store,s,object(),W+901000))
    assert sent==[False], 'UP prediction won, but the copied DOWN trade lost'
