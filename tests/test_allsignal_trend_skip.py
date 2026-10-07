"""After TWO losses in a row, never against the 15-minute trend (operator, 2026-10-02:
"the 15 minutes after 2 losses is the one I want live, nothing else changes to the live
rule"; FINDINGS 139-142). BTC only; the trend is Kalshi BRTI from brti_features, valid
at time t only if stamped AND received by t and at most 60 s old.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import main, messages  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import ExecutionResult  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

W = 1_790_600_400_000          # a window open (2026-09-28 12:20 ET-ish, mid-day)
X = W + 240_000                # the alert, 4 minutes in
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


def reference(tmp_path, move_bps, *, received_late_ms=0, stale=0, old_s=0):
    """A BRTI history: 100,000 fifteen minutes before the alert, then `move_bps`
    by the alert. Rows every 12 s, recorded under their own windows."""
    path = tmp_path / "settlement_reference.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE brti_features (window_open_ms INTEGER, received_ms INTEGER, "
               "ts_ms INTEGER, brti_value REAL, stale INTEGER)")
    start, end = X - 15 * 60_000 - 120_000, X - old_s * 1000
    t = start
    while t <= end:
        frac = min(1.0, max(0.0, (t - (X - 15 * 60_000)) / (15 * 60_000)))
        v = 100_000.0 * (1 + move_bps * frac / 10_000)
        db.execute("INSERT INTO brti_features VALUES (?,?,?,?,?)",
                   (t // 900_000 * 900_000, t + received_late_ms, t, v, stale))
        t += 12_000
    db.commit()
    db.close()
    return path


def setup(tmp_path, results, move_bps=-30.0, n=2, series="KXBTC15M", **ref):
    """`results`: the day's taken $ trades before W, oldest first (1 won, 0 lost)."""
    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    path = reference(tmp_path, move_bps, **ref)
    settings = Settings(kalshi_series=series, database_path=str(tmp_path / "btc15.db"),
                        allsignal_instruments="BTC,ETH", reference_database_path=str(path),
                        allsignal_trend_skip_after_losses=n)
    for i, won in enumerate(results):
        wo = W - 900_000 * (len(results) - i)
        store.allsignal_claim(wo, f"KXBTC15M-T{i}", "UP", 0.7, 8, 0.75, wo + 60_000)
        store.allsignal_finish(wo, "filled", filled=8, fill_price=0.7)
        store.db.execute("UPDATE allsignal_trades SET won=? WHERE window_open=?", (won, wo))
    store.db.commit()
    return store, settings, Client()


def contract(ask=0.72):
    return SimpleNamespace(ticker=TICKER, ask=lambda side: ask)


def snap(bps, target=100_000.0):
    return SimpleNamespace(price=target * (1 + bps / 10_000), target=target)


def alert(store, settings, client, side="UP", cushion=8.0):
    async def run():
        main.allsignal_on_alert(store, settings, client, contract(), side, 0.72,
                                snap(cushion if side == "UP" else -cushion), W, X)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(run())
    return store.db.execute("SELECT status, note FROM allsignal_trades WHERE window_open=?",
                            (W,)).fetchone()


def test_after_two_losses_an_up_signal_in_a_slide_is_skipped_and_said(tmp_path):
    store, settings, client = setup(tmp_path, [1, 0, 0], move_bps=-30.0)
    status, note = alert(store, settings, client, "UP")
    assert client.orders == [], "no order"
    assert status == "skipped"
    assert note == "after 2 losses in a row: UP is against the 15-min trend (-30.0 bps)"
    (row,) = store.allsignal_unannounced_misses(W)
    text = messages.allsignal_missed_message(asset="BTC", window_open=W, side="UP",
                                             signal_ask=0.72, limit=0.0, budget=6,
                                             reason=row["note"], skipped=True)
    assert "SKIPPED" in text and "against the 15-min trend" in text


def test_a_down_signal_against_a_rise_is_skipped_too(tmp_path):
    store, settings, client = setup(tmp_path, [0, 0], move_bps=+25.0)
    assert alert(store, settings, client, "DOWN")[0] == "skipped"


def test_with_the_trend_or_flat_it_trades_as_before(tmp_path):
    for move, side in ((-30.0, "DOWN"), (-9.0, "UP"), (+30.0, "UP")):
        d = tmp_path / f"{move}{side}"
        d.mkdir()
        store, settings, client = setup(d, [0, 0], move_bps=move)
        status, _ = alert(store, settings, client, side)
        assert status == "filled" and len(client.orders) == 1, (move, side)


def test_one_loss_is_not_enough(tmp_path):
    store, settings, client = setup(tmp_path, [1, 0], move_bps=-30.0)
    assert alert(store, settings, client, "UP")[0] == "filled", "the cushion's case, as before"


def test_a_win_breaks_the_streak(tmp_path):
    store, settings, client = setup(tmp_path, [0, 1, 0], move_bps=-30.0)
    assert alert(store, settings, client, "UP")[0] == "filled"


def test_skips_do_not_break_the_streak(tmp_path):
    store, settings, client = setup(tmp_path, [0, 0], move_bps=-30.0)
    wo = W - 450_000                                   # a skipped signal in between
    store.allsignal_claim(wo, "KXBTC15M-SK", "UP", 0.7, 0, 0.0, wo + 60_000)
    store.allsignal_finish(wo, "skipped", note="x")
    store.db.commit()
    assert main.allsignal_loss_streak(store, W, 2) is True


def test_unknown_trend_never_skips(tmp_path):
    for kw in ({"received_late_ms": 120_000}, {"stale": 1}, {"old_s": 90}):
        d = tmp_path / str(sorted(kw)[0])
        d.mkdir()
        store, settings, client = setup(d, [0, 0], move_bps=-30.0, **kw)
        assert main.brti_trend_bps(settings, X, 15) is None, kw
        assert alert(store, settings, client, "UP")[0] == "filled", kw


def test_the_trend_is_the_studys(tmp_path):
    store, settings, client = setup(tmp_path, [0, 0], move_bps=-30.0)
    assert main.brti_trend_bps(settings, X, 15) == pytest.approx(-30.0, abs=0.01)
    missing = SimpleNamespace(reference_database_path=str(tmp_path / "nope.db"))
    assert main.brti_trend_bps(missing, X, 15) is None
    assert not (tmp_path / "nope.db").exists(), "never creates a database"


def test_off_and_other_instruments_never_skip(tmp_path):
    for n, series in ((0, "KXBTC15M"), (2, "KXETH15M")):
        d = tmp_path / f"{n}{series}"
        d.mkdir()
        store, settings, client = setup(d, [0, 0], move_bps=-30.0, n=n, series=series)
        assert main.allsignal_trend_skip(store, settings, W, "UP", X) == ""


def test_an_error_lets_the_signal_through(tmp_path, monkeypatch):
    store, settings, client = setup(tmp_path, [0, 0], move_bps=-30.0)
    monkeypatch.setattr(main, "brti_trend_bps", lambda *a: 1 / 0)
    assert main.allsignal_trend_skip(store, settings, W, "UP", X) == ""


def test_the_settings():
    d = Settings.model_fields
    assert d["allsignal_trend_skip_after_losses"].default == 0, "off unless set"
    assert d["allsignal_trend_skip_minutes"].default == 15
    assert d["allsignal_trend_skip_bps"].default == 10.0


def test_unknown_results_never_arm_it(tmp_path):
    """Review 2026-10-02: two WINNERS still unsettled at 09-29 21:34 would have armed it,
    and the skip is final. Only known losses arm it; the cushion still waits as before."""
    store, settings, client = setup(tmp_path, [0, 0], move_bps=-30.0)
    store.db.execute("UPDATE allsignal_trades SET won=NULL")
    store.db.commit()
    assert main.allsignal_loss_streak(store, W, 2) is False
    assert main.allsignal_trend_skip(store, settings, W, "UP", X) == ""
    store.db.execute("UPDATE allsignal_trades SET won=0 WHERE window_open=?", (W - 1_800_000,))
    store.db.commit()
    assert main.allsignal_loss_streak(store, W, 2) is False, "one known loss + one unknown"


def test_a_settled_loss_without_a_grade_still_counts(tmp_path):
    store, settings, client = setup(tmp_path, [0, 0], move_bps=-30.0)
    store.db.execute("UPDATE allsignal_trades SET won=NULL")
    for i in range(2):
        store.db.execute("INSERT INTO settlements (ticker, market_result) VALUES (?, 'no')",
                         (f"KXBTC15M-T{i}",))
    store.db.commit()
    assert main.allsignal_loss_streak(store, W, 2) is True, "UP that settled 'no' lost"


def test_a_locked_database_never_raises_out_of_the_alert(tmp_path, monkeypatch):
    store, settings, client = setup(tmp_path, [0, 0], move_bps=-30.0)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "allsignal_claim", locked)
    alert(store, settings, client, "UP")                 # must not raise
    assert client.orders == []
