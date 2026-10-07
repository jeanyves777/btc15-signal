"""Only BTC and gold send alerts; the shadows send a summary (operator, 2026-09-28).

"On telegram send just the alerts for Gold and Btc. All the other should just
come in as summary while in shadow." A shadow instance records everything and
keeps quiet except for money that actually moved; the command-listening
instance sends one SHADOW SUMMARY per session close.
"""

import asyncio
import inspect
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import main, messages  # noqa: E402
from btc15_signal import shadow_summary as S  # noqa: E402
from btc15_signal.notify import Notifier  # noqa: E402
from btc15_signal.store import Store  # noqa: E402


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send(self, text, buttons=None):
        self.sent.append(text)
        return len(self.sent)


def notifier(tmp_path, series, *, stored_auto=None, listed="BTC,GOLD"):
    store = Store(str(tmp_path / f"{series}.db"))
    if stored_auto is not None:
        store.set_setting("auto_trade_enabled", 1.0 if stored_auto else 0.0, 1)
    settings = SimpleNamespace(telegram_alert_instruments=listed,
                               kalshi_series=series, auto_trade_enabled=False)
    return Notifier(FakeTelegram(), store, settings)


def send(n, kind, money=False):
    return asyncio.run(n.send_once(kind, f"{kind}-k", "x", 1, money=money))


def test_btc_and_gold_send_everything(tmp_path):
    for series in ("KXBTC15M", "KXGOLD15M"):
        n = notifier(tmp_path, series, stored_auto=False)   # even with auto off
        assert n.alerts_on()
        assert send(n, "signal") and send(n, "learning")


def test_a_shadow_sends_only_money_and_the_summary(tmp_path):
    n = notifier(tmp_path, "KXETH15M", stored_auto=False)
    assert not n.alerts_on()
    for quiet in ("signal", "automation_off", "settlement", "session_close",
                  "learning", "hypotheses", "market_gap", "recovery"):
        assert send(n, quiet) is False, quiet
    for money in ("fill", "not_filled", "cash_out", "auto_exit", "exit_warning",
                  "combo", "combo_result", "shadow_summary"):
        assert send(n, money) is True, money
    assert send(n, "settlement", money=True) is True, "a settled TRADE is money"
    assert len(n.telegram.sent) == 9


def test_a_shadow_that_starts_trading_alerts_again(tmp_path):
    n = notifier(tmp_path, "KXSOL15M", stored_auto=True)
    assert n.alerts_on() and send(n, "signal")


def test_an_empty_list_keeps_the_old_behaviour(tmp_path):
    n = notifier(tmp_path, "KXXRP15M", stored_auto=False, listed="")
    assert n.alerts_on()


def test_the_service_wires_both_ends():
    src = " ".join(inspect.getsource(main).split())
    assert "money=bool(trade)," in src
    assert "if shadow_summary_sender(settings):" in src
    sender = SimpleNamespace(telegram_alert_instruments="BTC,GOLD", kalshi_series="KXBTC15M")
    assert main.shadow_summary_sender(sender)
    assert not main.shadow_summary_sender(SimpleNamespace(
        telegram_alert_instruments="BTC,GOLD", kalshi_series="KXGOLD15M"))
    assert not main.shadow_summary_sender(SimpleNamespace(
        telegram_alert_instruments="", kalshi_series="KXBTC15M"))
    # Since 2026-09-28 the session summary carries the new strategy only.
    assert "shadow_summary.collect_allsignal(" in src
    assert '"shadow_summary", f"{today_key}:{closed}"' in src


# ------------------------------------------------------------- the summary

def test_session_bounds_are_the_session_that_just_closed():
    import datetime as dt

    now = int(dt.datetime(2026, 9, 28, 7, 0, 5, tzinfo=dt.UTC).timestamp() * 1000)
    start, end = S.session_bounds("asia", now)
    assert (end - start) == 7 * 3_600_000 and end == now - 5_000
    late = int(dt.datetime(2026, 9, 29, 0, 0, 5, tzinfo=dt.UTC).timestamp() * 1000)
    s2, e2 = S.session_bounds("late-us", late)
    assert e2 == late - 5_000 and e2 - s2 == 3 * 3_600_000


def shadow_store(path, auto=None, rows=()):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value REAL, updated_at INTEGER)")
    if auto is not None:
        con.execute("INSERT INTO settings VALUES ('auto_trade_enabled', ?, 1)", (float(auto),))
    con.execute("CREATE TABLE observations (window_open INTEGER, observed_ms INTEGER, "
                "side TEXT, our_ask REAL, won INTEGER, alerted INTEGER)")
    con.execute("CREATE TABLE intelligence_decisions (window_open INTEGER, "
                "decided_ms INTEGER, base_qualified INTEGER, final_action TEXT, "
                "ask REAL, band_hold_s INTEGER, side TEXT, won INTEGER)")
    for w, ask, won, qualified in rows:
        con.execute("INSERT INTO observations VALUES (?,?,?,?,?,1)", (w, w + 1, "UP", ask, won))
        if qualified:
            con.execute("INSERT INTO intelligence_decisions VALUES (?,?,1,'neutral',?,90,'UP',?)",
                        (w, w + 2, ask, won))
    con.commit()
    con.close()


def test_the_summary_grades_alerts_and_would_trade_decisions(tmp_path):
    w0 = 1_790_553_600_000
    shadow_store(tmp_path / "eth15.db", auto=0, rows=[
        (w0, 0.70, 1, True), (w0 + 900_000, 0.80, 0, True),
        (w0 + 1_800_000, 0.75, 1, False), (w0 + 2_700_000, 0.72, None, False)])
    shadow_store(tmp_path / "sol15.db", auto=1)              # trading: not a shadow
    shadow_store(tmp_path / "xrp15.db")                      # no flag: shadow
    shadow_store(tmp_path / "gold15.db", auto=0)             # listed: not a shadow
    assert S.shadows(tmp_path, {"BTC", "GOLD"}) == ["ETH", "XRP"]
    s = S.stats(tmp_path / "eth15.db", w0, w0 + 4 * 900_000)
    assert s["alerts"]["n"] == 3 and s["alerts"]["wins"] == 2 and s["alerts"]["open"] == 1
    assert round(s["alerts"]["pnl"], 2) == round((1 - .70) + (0 - .80) + (1 - .75), 2)
    assert s["rule"]["n"] == 2 and s["rule"]["wins"] == 1
    text = messages.shadow_summary_message(session="asia", ny_day="2026-09-28",
                                           rows=[("ETH", s)])
    assert "SESSION SUMMARY" in text and "<b>ETH</b> alerts: 3" in text
    assert "rule would have traded: 2" in text and "no money moved" in text
