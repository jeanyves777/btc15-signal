"""Price confirmation accepts 85-90c only: 91c and higher keep waiting, orders and chases stay <= 90c."""
import asyncio

import pytest

from btc15_signal import main
from btc15_signal.config import Settings
from test_allsignal_ohlc_lock import W, X, TICKER, setup, contract, snap, alert, poll, row, events


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(main.asyncio, "sleep", instant)
    main.ALLSIGNAL_WAIT.clear()
    main.ALLSIGNAL_WAIT_NOTE.clear()
    main.LOCK_WAIT.clear()
    yield
    main.ALLSIGNAL_WAIT.clear()
    main.LOCK_WAIT.clear()


def configured(tmp_path, **kw):
    return setup(tmp_path, allsignal_skip_wait_min_ask=.85, allsignal_skip_wait_max_ask=.90,
                 allsignal_ohlc_lock_wait=False, **kw)


def limit(store):
    return store.db.execute("SELECT limit_price FROM allsignal_trades WHERE window_open=?",
                            (W,)).fetchone()[0]


def test_91c_is_rejected_and_the_wait_goes_on_until_back_in_range(tmp_path):
    store, s, client = configured(tmp_path)
    alert(store, s, client, .70)
    poll(store, s, client, .91, X + 10_000, 650, down=.09)
    poll(store, s, client, .95, X + 20_000, 640, down=.05)
    assert not client.orders and W in main.LOCK_WAIT and row(store) is None
    poll(store, s, client, .90, X + 30_000, 630, down=.10)
    assert [o[0] for o in client.orders] == ["UP"] and row(store)[0] == "filled"


def test_the_range_is_85_through_90_inclusive(tmp_path):
    for i, (ask, enters) in enumerate(((.84, False), (.85, True), (.88, True), (.90, True), (.91, False))):
        d = tmp_path / str(i)
        d.mkdir()
        main.LOCK_WAIT.clear()
        store, s, client = configured(d)
        alert(store, s, client, .70)
        poll(store, s, client, ask, X + 10_000, 650, down=round(1 - ask, 2))
        assert bool(client.orders) is enters, ask


def test_the_opposite_side_flips_only_inside_the_range(tmp_path):
    store, s, client = configured(tmp_path)
    alert(store, s, client, .70)
    poll(store, s, client, .08, X + 10_000, 650, down=.92)
    assert not client.orders, "DOWN at 92c is above the ceiling"
    poll(store, s, client, .11, X + 20_000, 640, down=.89)
    assert [o[0] for o in client.orders] == ["DOWN"]


def test_an_alert_already_above_the_ceiling_waits(tmp_path):
    store, s, client = configured(tmp_path)
    alert(store, s, client, .95)
    assert not client.orders and W in main.LOCK_WAIT


def test_the_order_limit_never_exceeds_90c_and_fills_may_be_cheaper(tmp_path):
    for i, ask in enumerate((.85, .88, .90)):
        d = tmp_path / str(i)
        d.mkdir()
        main.LOCK_WAIT.clear()
        store, s, client = configured(d)
        alert(store, s, client, ask)
        assert len(client.orders) == 1, ask
        assert limit(store) == pytest.approx(.90), ask


def test_no_ceiling_by_default(tmp_path):
    assert Settings.model_fields["allsignal_skip_wait_max_ask"].default == 0.0
    store, s, client = setup(tmp_path, allsignal_skip_wait_min_ask=.85, allsignal_ohlc_lock_wait=False)
    assert main._price_wait_ceiling(s) == 0.0
    alert(store, s, client, .95)
    assert len(client.orders) == 1


def test_the_ceiling_needs_an_active_floor_above_it():
    def ceiling(lo, hi):
        return main._price_wait_ceiling(Settings(kalshi_series="KXBTC15M", allsignal_skip_wait_min_ask=lo,
                                                 allsignal_skip_wait_max_ask=hi))

    assert ceiling(0.0, .90) == 0.0
    assert ceiling(.85, .80) == 0.0
    assert ceiling(.85, .90) == pytest.approx(.90)
    eth = Settings(kalshi_series="KXETH15M", allsignal_skip_wait_min_ask=.85, allsignal_skip_wait_max_ask=.90)
    assert main._price_wait_ceiling(eth) == 0.0


def test_retry_and_chase_never_buy_above_the_ceiling(tmp_path):
    store, s, client = configured(tmp_path, allsignal_chase_max=.93)
    alert(store, s, client, .87)
    assert len(client.orders) == 1
    store.db.execute("UPDATE allsignal_trades SET status='unfilled', order_id='x', filled=0, "
                     "attempt_ms=?, reported_ms=NULL WHERE window_open=?", (X - 120_000, W))
    store.db.commit()

    def retry(ask):
        async def run():
            main.allsignal_retry_poll(store, s, client, contract(ask, round(1 - ask, 2)), snap(),
                                      W, 400, X + 200_000)
            await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
        asyncio.run(run())

    retry(.92)
    assert len(client.orders) == 1, "a 92c chase is above the 90c ceiling"
    retry(.90)
    assert len(client.orders) == 2, "a retry at 90c is inside the range"
    assert limit(store) <= .90 + 1e-9
