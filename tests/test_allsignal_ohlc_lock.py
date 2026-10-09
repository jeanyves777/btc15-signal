"""A BTC signal made while a chop range is locked is logged and held until either side's ask
reaches 85c; the opposite side is taken if it gets there first."""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import main  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import ExecutionResult  # noqa: E402
from btc15_signal.ohlc_regime import range_lock  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

W = 1_790_600_400_000
X = W + 240_000
HOURS = 8
TICKER = "KXBTC15M-26SEP281235-35"


class Client:
    def __init__(self):
        self.orders = []

    async def execute_with_take_profit(self, order, slippage, ceiling=None):
        self.orders.append((order.side, order.entry_limit))
        return ExecutionResult("filled", order.count, "oid", None, "filled")

    async def fill_detail(self, order_id, side):
        return None


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(main.asyncio, "sleep", instant)
    main.ALLSIGNAL_WAIT.clear()
    main.ALLSIGNAL_WAIT_NOTE.clear()
    main.LOCK_WAIT.clear()


def reference(tmp_path, price_at, name="ref.db"):
    path = tmp_path / name
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE brti_features (id INTEGER PRIMARY KEY, window_open_ms INTEGER, "
               "received_ms INTEGER, ts_ms INTEGER, brti_value REAL, stale INTEGER)")
    t = X - HOURS * 3_600_000
    while t <= X:
        db.execute("INSERT INTO brti_features (window_open_ms, received_ms, ts_ms, brti_value, "
                   "stale) VALUES (?,?,?,?,0)", (t // 900_000 * 900_000, t, t, price_at(t)))
        t += 12_000
    db.commit()
    db.close()
    return path


def flat(_t):
    return 100_000.0


def rising(t):
    return 100_000.0 + (t - (X - HOURS * 3_600_000)) / 1000 * 3


def settings(path, tmp_path, **kw):
    return Settings(kalshi_series="KXBTC15M", reference_database_path=str(path),
                    database_path=str(tmp_path / "btc15.db"),
                    allsignal_instruments="BTC,ETH", **kw)


def setup(tmp_path, price_at=flat, **kw):
    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    path = reference(tmp_path, price_at)
    return store, settings(path, tmp_path, **kw), Client()


def contract(ask, down=None):
    down = round(1 - ask, 2) if down is None else down
    return SimpleNamespace(ticker=TICKER, ask=lambda side: ask if side == "UP" else down)


def snap(bps=8.0, target=100_000.0):
    return SimpleNamespace(price=target * (1 + bps / 10_000), target=target)


def alert(store, settings, client, ask, now=X):
    async def run():
        main.allsignal_on_alert(store, settings, client, contract(ask), "UP", ask, snap(), W, now)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(run())


def poll(store, settings, client, ask, now, remaining, down=None):
    async def run():
        main.allsignal_lock_poll(store, settings, client, contract(ask, down), snap(), W,
                                 remaining, now)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(run())


def events(settings):
    path = Path(settings.reference_database_path).parent / "lock_shadow.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def row(store):
    return store.db.execute("SELECT status, note FROM allsignal_trades WHERE window_open=?",
                            (W,)).fetchone()


def test_a_flat_range_locks_and_a_trend_does_not(tmp_path):
    path = reference(tmp_path, flat)
    assert range_lock(path, X)["locked"] is True
    assert main.allsignal_ohlc_lock_note(settings(path, tmp_path, allsignal_ohlc_lock_wait=True),
                                         X).startswith("chop range locked")
    path = reference(tmp_path, rising, "rising.db")
    assert range_lock(path, X)["locked"] is False
    assert main.allsignal_ohlc_lock_note(settings(path, tmp_path, allsignal_ohlc_lock_wait=True),
                                         X) == ""


def test_a_locked_signal_below_the_floor_waits_and_is_not_skipped(tmp_path):
    store, s, client = setup(tmp_path, allsignal_ohlc_lock_wait=True)
    alert(store, s, client, 0.72)
    assert client.orders == [] and row(store) is None, "held: no order, and no skipped row"
    assert W in main.LOCK_WAIT
    (e,) = events(s)
    assert e["event"] == "locked" and e["held"] is True and e["floor"] == 0.85


def test_it_enters_when_the_ask_reaches_the_floor(tmp_path):
    store, s, client = setup(tmp_path, allsignal_ohlc_lock_wait=True)
    alert(store, s, client, 0.72)
    poll(store, s, client, 0.80, X + 30_000, 600)
    assert client.orders == [], "still under 85c"
    poll(store, s, client, 0.86, X + 60_000, 560)
    assert [o[0] for o in client.orders] == ["UP"] and W not in main.LOCK_WAIT
    assert row(store)[0] == "filled"
    assert [e["event"] for e in events(s)] == ["locked", "entered"]
    assert events(s)[-1]["flipped"] is False


def test_the_opposite_side_reaching_the_floor_is_taken_instead(tmp_path):
    store, s, client = setup(tmp_path, allsignal_ohlc_lock_wait=True)
    alert(store, s, client, 0.72)
    poll(store, s, client, 0.12, X + 90_000, 500, down=0.87)
    assert [o[0] for o in client.orders] == ["DOWN"]
    assert row(store)[0] == "filled"
    e = events(s)[-1]
    assert e["event"] == "entered" and e["flipped"] is True and e["side"] == "DOWN"


def test_neither_side_at_the_floor_by_the_deadline_is_no_entry_and_said(tmp_path):
    store, s, client = setup(tmp_path, allsignal_ohlc_lock_wait=True)
    alert(store, s, client, 0.72)
    poll(store, s, client, 0.74, X + 400_000, 119, down=0.28)
    assert client.orders == [] and W not in main.LOCK_WAIT
    status, note = row(store)
    assert status == "skipped" and "neither side reached 85" in note
    assert events(s)[-1]["event"] == "passed"


def test_an_alert_already_at_the_floor_enters_at_once(tmp_path):
    store, s, client = setup(tmp_path, allsignal_ohlc_lock_wait=True)
    alert(store, s, client, 0.87)
    assert len(client.orders) == 1 and not main.LOCK_WAIT
    assert events(s)[0]["held"] is False


def test_off_log_only_and_other_cases_trade_as_before(tmp_path):
    assert Settings.model_fields["allsignal_ohlc_lock_wait"].default is False
    store, s, client = setup(tmp_path, allsignal_ohlc_lock_wait=False)
    alert(store, s, client, 0.72)
    assert len(client.orders) == 1 and not main.LOCK_WAIT, "off: as before"
    d = tmp_path / "logonly"
    d.mkdir()
    store, s, client = setup(d, allsignal_ohlc_lock_wait=True, allsignal_ohlc_lock_min_ask=0.0)
    alert(store, s, client, 0.72)
    assert len(client.orders) == 1 and events(s)[0]["held"] is False, "min_ask 0 logs only"
    d = tmp_path / "trend"
    d.mkdir()
    store, s, client = setup(d, rising, allsignal_ohlc_lock_wait=True)
    alert(store, s, client, 0.72)
    assert len(client.orders) == 1 and not main.LOCK_WAIT, "no lock, no wait"


def test_missing_data_and_errors_never_hold(tmp_path, monkeypatch):
    path = reference(tmp_path, flat)
    eth = Settings(kalshi_series="KXETH15M", reference_database_path=str(path),
                   allsignal_ohlc_lock_wait=True)
    assert main.allsignal_ohlc_lock_note(eth, X) == ""
    missing = settings(tmp_path / "nope.db", tmp_path, allsignal_ohlc_lock_wait=True)
    assert main.allsignal_ohlc_lock_note(missing, X) == ""
    assert not (tmp_path / "nope.db").exists(), "never creates a database"
    sparse = reference(tmp_path, flat, "sparse.db")
    db = sqlite3.connect(sparse)
    db.execute("DELETE FROM brti_features WHERE ts_ms < ?", (X - 2 * 3_600_000,))
    db.commit()
    db.close()
    assert range_lock(sparse, X)["locked"] is False, "unknown is never chop"
    import btc15_signal.ohlc_regime as regime
    monkeypatch.setattr(regime, "range_lock", lambda *a, **k: 1 / 0)
    assert main.allsignal_ohlc_lock_note(
        settings(path, tmp_path, allsignal_ohlc_lock_wait=True), X) == ""
