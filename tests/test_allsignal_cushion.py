"""After a losing trade, the next signal waits for a 5 bps cushion (operator,
2026-09-29: "adopt 5 and ship it live"; FINDINGS 112)."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import main, messages  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import ExecutionResult  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

W = 1_790_600_400_000          # a window open
PREV = W - 900_000             # the window before it
TICKER = "KXBTC15M-26SEP281215-15"


class Client:
    def __init__(self):
        self.orders = []

    async def execute_with_take_profit(self, order, slippage, ceiling=None):
        self.orders.append((order.side, order.entry_limit, ceiling))
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


def setup(tmp_path, last=None):
    """`last`: None (no trade yet today), 1 (won) or 0 (lost)."""
    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    settings = Settings(kalshi_series="KXBTC15M", database_path=str(tmp_path / "btc15.db"),
                        allsignal_instruments="BTC")
    if last is not None:
        store.allsignal_claim(PREV, "KXBTC15M-26SEP281200-00", "UP", 0.7, 1, 0.75, PREV + 60_000)
        store.allsignal_finish(PREV, "filled", filled=1, fill_price=0.7)
        store.db.execute("UPDATE allsignal_trades SET won=?, pnl=? WHERE window_open=?",
                         (last, 0.3 if last else -0.7, PREV))
        store.db.commit()
    return store, settings, Client()


def contract(ask=0.72):
    return SimpleNamespace(ticker=TICKER, ask=lambda side: ask)


def snap(bps, target=100_000.0):
    """A reference price `bps` above the target (UP side)."""
    return SimpleNamespace(price=target * (1 + bps / 10_000), target=target)


def go(fn):
    async def run():
        fn()
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(run())


def test_after_a_loss_it_waits_then_enters_on_the_first_cushioned_poll(tmp_path):
    store, settings, client = setup(tmp_path, last=0)
    go(lambda: main.allsignal_on_alert(store, settings, client, contract(0.70), "UP", 0.70,
                                       snap(2.0), W, W + 240_000))
    assert client.orders == [], "2 bps is not enough after a loss"
    assert W in main.ALLSIGNAL_WAIT
    go(lambda: main.allsignal_cushion_poll(store, settings, client, contract(0.74), snap(4.9),
                                           W, 500, W + 260_000))
    assert client.orders == []
    go(lambda: main.allsignal_cushion_poll(store, settings, client, contract(0.76), snap(5.3),
                                           W, 480, W + 280_000))
    assert [o[0] for o in client.orders] == ["UP"]
    assert client.orders[0][1] == 0.76, "at that moment's price"
    assert "waited 40s for 5.3 bps" in main.ALLSIGNAL_WAIT_NOTE[W]
    assert W not in main.ALLSIGNAL_WAIT


def test_after_a_win_or_first_of_the_day_it_enters_at_the_alert(tmp_path):
    for last in (1, None):
        (tmp_path / str(last)).mkdir()
        store, settings, client = setup(tmp_path / str(last), last=last)
        go(lambda: main.allsignal_on_alert(store, settings, client, contract(), "UP", 0.72,
                                           snap(1.0), W, W + 240_000))
        assert len(client.orders) == 1 and W not in main.ALLSIGNAL_WAIT


def test_after_a_loss_with_the_cushion_already_there_it_enters_at_once(tmp_path):
    store, settings, client = setup(tmp_path, last=0)
    go(lambda: main.allsignal_on_alert(store, settings, client, contract(), "UP", 0.72,
                                       snap(6.0), W, W + 240_000))
    assert len(client.orders) == 1
    assert "6.0 bps clear of the line at the alert" in main.ALLSIGNAL_WAIT_NOTE[W]


def test_never_clear_with_2_minutes_left_is_skipped_and_said(tmp_path):
    store, settings, client = setup(tmp_path, last=0)
    go(lambda: main.allsignal_on_alert(store, settings, client, contract(), "UP", 0.72,
                                       snap(1.0), W, W + 240_000))
    go(lambda: main.allsignal_cushion_poll(store, settings, client, contract(), snap(3.1),
                                           W, 300, W + 600_000))
    go(lambda: main.allsignal_cushion_poll(store, settings, client, contract(), snap(2.0),
                                           W, 110, W + 790_000))
    assert client.orders == []
    status, note = store.db.execute(
        "SELECT status, note FROM allsignal_trades WHERE window_open=?", (W,)).fetchone()
    assert status == "skipped" and "never 5 bps clear" in note and "best 3.1 bps" in note
    (row,) = store.allsignal_unannounced_misses(W)
    text = messages.allsignal_missed_message(
        asset="BTC", window_open=W, side="UP", signal_ask=0.72, limit=0.0,
        budget=5, reason=row["note"], skipped=True)
    assert "SKIPPED" in text and "never 5 bps clear" in text


def test_the_cushion_is_on_the_signals_side():
    below = SimpleNamespace(price=99_950.0, target=100_000.0)     # 5 bps under
    assert main.cushion_bps(below, "DOWN") == pytest.approx(5.0)
    assert main.cushion_bps(below, "UP") == pytest.approx(-5.0)


def test_an_unknown_last_result_counts_as_a_loss(tmp_path):
    store, settings, client = setup(tmp_path, last=0)
    store.db.execute("UPDATE allsignal_trades SET won=NULL WHERE window_open=?", (PREV,))
    store.db.commit()
    assert main.allsignal_after_loss(store, W) is True


def test_zero_turns_it_off(tmp_path):
    store, settings, client = setup(tmp_path, last=0)
    settings = settings.model_copy(update={"allsignal_after_loss_cushion_bps": 0.0})
    go(lambda: main.allsignal_on_alert(store, settings, client, contract(), "UP", 0.72,
                                       snap(0.5), W, W + 240_000))
    assert len(client.orders) == 1


# ------------------------------------------- review fixes, 2026-09-29 23:5x

def test_a_late_result_that_comes_in_as_a_win_enters_at_once(tmp_path):
    store, settings, client = setup(tmp_path, last=0)
    store.db.execute("UPDATE allsignal_trades SET won=NULL WHERE window_open=?", (PREV,))
    store.db.commit()
    go(lambda: main.allsignal_on_alert(store, settings, client, contract(0.72), "UP", 0.72,
                                       snap(1.0), W, W + 240_000))
    assert client.orders == [] and W in main.ALLSIGNAL_WAIT, "unknown counts as a loss"
    store.db.execute("UPDATE allsignal_trades SET won=1 WHERE window_open=?", (PREV,))
    store.db.commit()
    go(lambda: main.allsignal_cushion_poll(store, settings, client, contract(0.73), snap(1.0),
                                           W, 600, W + 250_000))
    assert [o[1] for o in client.orders] == [0.73]
    assert "came in late as a win" in main.ALLSIGNAL_WAIT_NOTE[W]


def test_a_check_that_errors_at_the_alert_waits_instead_of_entering(tmp_path, monkeypatch):
    store, settings, client = setup(tmp_path, last=0)

    def boom(*_a):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(main, "allsignal_after_loss", boom)
    go(lambda: main.allsignal_on_alert(store, settings, client, contract(), "UP", 0.72,
                                       snap(1.0), W, W + 240_000))
    assert client.orders == [] and W in main.ALLSIGNAL_WAIT


def test_a_wait_survives_a_restart(tmp_path):
    store, settings, client = setup(tmp_path, last=0)
    go(lambda: main.allsignal_on_alert(store, settings, client, contract(), "UP", 0.72,
                                       snap(1.0), W, W + 240_000))
    main.ALLSIGNAL_WAIT.clear()                       # the process restarted
    go(lambda: main.allsignal_cushion_poll(store, settings, client, contract(0.77), snap(5.5),
                                           W, 500, W + 300_000))
    assert [o[1] for o in client.orders] == [0.77]
    assert not store.get_setting_text(main.WAIT_KEY), "the saved wait is cleared"


def test_a_wait_whose_window_ended_is_recorded_as_skipped(tmp_path):
    store, settings, client = setup(tmp_path, last=0)
    go(lambda: main.allsignal_on_alert(store, settings, client, contract(), "UP", 0.72,
                                       snap(1.0), W, W + 240_000))
    go(lambda: main.allsignal_cushion_poll(store, settings, client, contract(), snap(1.0),
                                           W + 900_000, 880, W + 920_000))
    status, note = store.db.execute(
        "SELECT status, note FROM allsignal_trades WHERE window_open=?", (W,)).fetchone()
    assert status == "skipped" and "interrupted" in note
    assert client.orders == []


# ---------------------------------------- safety-review fixes, 2026-09-30

def test_a_stale_saved_wait_never_relabels_a_real_fill(tmp_path, monkeypatch):
    store, settings, client = setup(tmp_path, last=0)
    go(lambda: main.allsignal_on_alert(store, settings, client, contract(), "UP", 0.72,
                                       snap(1.0), W, W + 240_000))
    real_save = main._save_wait
    monkeypatch.setattr(main, "_save_wait", lambda store, wait: None if wait is None
                        else real_save(store, wait))          # the clearing write fails
    go(lambda: main.allsignal_cushion_poll(store, settings, client, contract(0.75), snap(6.0),
                                           W, 500, W + 300_000))
    assert len(client.orders) == 1
    monkeypatch.setattr(main, "_save_wait", real_save)
    go(lambda: main.allsignal_cushion_poll(store, settings, client, contract(), snap(1.0),
                                           W, 100, W + 800_000))
    status = store.db.execute("SELECT status FROM allsignal_trades WHERE window_open=?",
                              (W,)).fetchone()[0]
    assert status == "filled", "the stale wait must not relabel the fill"
    assert len(client.orders) == 1


def test_a_late_win_does_not_buy_against_the_price(tmp_path):
    store, settings, client = setup(tmp_path, last=0)
    store.db.execute("UPDATE allsignal_trades SET won=NULL WHERE window_open=?", (PREV,))
    store.db.commit()
    go(lambda: main.allsignal_on_alert(store, settings, client, contract(), "UP", 0.72,
                                       snap(1.0), W, W + 240_000))
    store.db.execute("UPDATE allsignal_trades SET won=1 WHERE window_open=?", (PREV,))
    store.db.commit()
    go(lambda: main.allsignal_cushion_poll(store, settings, client, contract(0.18), snap(-8.0),
                                           W, 500, W + 300_000))
    assert client.orders == [] and W in main.ALLSIGNAL_WAIT, "price is below the line"


def test_a_skipped_message_shows_no_order_price():
    text = messages.allsignal_missed_message(
        asset="BTC", window_open=W, side="UP", signal_ask=0.72, limit=0.0, budget=5,
        reason="after a loss: never 5 bps clear", skipped=True)
    assert "limit" not in text and "Signal 72" in text


def test_only_btc_waits(tmp_path):
    store, settings, client = setup(tmp_path, last=0)
    gold = settings.model_copy(update={"kalshi_series": "KXGOLD15M",
                                       "allsignal_instruments": "GOLD"})
    go(lambda: main.allsignal_on_alert(store, gold, client, contract(), "UP", 0.72,
                                       snap(0.5), W, W + 240_000))
    assert len(client.orders) == 1 and W not in main.ALLSIGNAL_WAIT
