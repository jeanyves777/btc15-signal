"""The $ signal messages show the Kalshi target price (operator, 2026-10-03: "Can we have
the signal in telegram show the Kalshi target price for clarity"). Display only."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import main, messages  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

W = 1_791_084_600_000          # 2026-10-03 21:30 ET - the real window used below
TICKER = "KXBTC15M-26OCT032145-45"


def test_the_entry_shows_the_target_what_the_side_needs_and_btc_at_the_signal():
    text = messages.allsignal_trade_message(
        asset="BTC", window_open=W, side="UP", signal_ask=0.74, fill_price=0.70, count=35,
        mirrored="", budget=25, target=84787.45, ref=84810.906)
    lines = text.splitlines()
    assert lines[2].startswith("\U0001f3af Signal 74")
    assert lines[3] == "\U0001f4cd Kalshi target <b>$84,787.45</b> · UP wins at or above it"
    assert lines[4] == "\U0001f4b2 BTC at the signal $84,810.91 (+2.8 bps above)"
    assert lines[5].startswith("\U0001f4b5 Cost")
    down = messages.allsignal_trade_message(
        asset="BTC", window_open=W, side="DOWN", signal_ask=0.74, fill_price=0.70, count=35,
        mirrored="", budget=25, target=84862.28, ref=84852.215)
    assert "DOWN wins below it" in down and "(-1.2 bps below)" in down


def test_the_result_shows_where_it_settled_against_the_target():
    text = messages.allsignal_settled_message(
        asset="BTC", window_open=W, side="UP", fill_price=0.70, won=True, pnl=9.41,
        count=35, budget=25, target=84787.45, final=84862.28)
    assert "\U0001f4cd Kalshi target <b>$84,787.45</b> → settled <b>$84,862.28</b> " \
           "(+8.8 bps above)" in text
    only = messages.allsignal_settled_message(
        asset="BTC", window_open=W, side="UP", fill_price=0.70, won=True, pnl=9.41,
        count=35, budget=25, target=84787.45)
    assert "Kalshi target <b>$84,787.45</b> · UP wins at or above it" in only


def test_a_skipped_signal_shows_it_too():
    text = messages.allsignal_missed_message(
        asset="BTC", window_open=W, side="UP", signal_ask=0.76, limit=0.0, budget=25,
        reason="after a loss: never 5 bps clear", skipped=True, target=84787.45, ref=84800.0)
    assert "Kalshi target <b>$84,787.45</b>" in text and "BTC at the signal $84,800.00" in text
    assert text.splitlines()[-1].startswith("⚠")


def test_without_a_target_the_messages_are_as_before():
    kw = dict(asset="BTC", window_open=W, side="UP", signal_ask=0.74, fill_price=0.70,
              count=35, mirrored="", budget=25)
    assert "Kalshi target" not in messages.allsignal_trade_message(**kw)
    assert messages.target_lines("UP", None, 1.0) == []
    assert messages.target_lines("UP", "x", 1.0) == [], "never raises"
    assert messages.target_lines("UP", 0.5432, 0.55)[0].endswith("$0.5432</b> · UP wins at or above it")


def test_the_prices_come_from_what_the_service_recorded(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    assert main._window_prices(store, W, TICKER) == (None, None, None)
    store.record_alert("primary", W, W + 240_000)
    row = {"window_open": W, "observed_ms": W + 240_000, "target": 84787.45, "btc": 84810.906}
    for _, col, kind, notnull, default, pk in store.db.execute("PRAGMA table_info(observations)"):
        if notnull and default is None and col not in row:
            row[col] = "" if "TEXT" in (kind or "").upper() else 0
    store.db.execute(f"INSERT INTO observations ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                     tuple(row.values()))
    store.db.execute("INSERT INTO settlements (ticker, strike, expiration_value) VALUES (?,?,?)",
                     (TICKER, 84787.45, 84862.28))
    store.db.commit()
    assert main._window_prices(store, W, TICKER) == (84787.45, 84810.906, 84862.28)
    assert main._window_prices(store, W) == (84787.45, 84810.906, None)


def test_a_broken_store_never_breaks_a_message():
    class Broken:
        @property
        def db(self):
            raise RuntimeError("locked")

    assert main._window_prices(Broken(), W, TICKER) == (None, None, None)


def test_before_the_settlement_sync_the_next_windows_strike_is_the_settled_value(tmp_path):
    """Review 2026-10-03: the sync lands ~60-70 s after the result message; Kalshi's next
    window opens at this one's settled value (strike[W+15m] == expiration_value[W])."""
    store = Store(str(tmp_path / "btc15.db"))
    store.record_alert("primary", W, W + 240_000)
    cols = [(c[1], c[2], c[3], c[4]) for c in store.db.execute("PRAGMA table_info(observations)")]

    def obs(window, observed, target, btc):
        row = {"window_open": window, "observed_ms": observed, "target": target, "btc": btc}
        for col, kind, notnull, default in cols:
            if notnull and default is None and col not in row:
                row[col] = "" if "TEXT" in (kind or "").upper() else 0
        store.db.execute(f"INSERT INTO observations ({','.join(row)}) VALUES "
                         f"({','.join('?' * len(row))})", tuple(row.values()))

    obs(W, W + 240_000, 84787.45, 84810.906)
    store.db.commit()
    assert main._window_prices(store, W, TICKER)[2] is None, "nothing known yet"
    obs(W + 900_000, W + 900_000 + 20_000, 84862.28, 84862.0)        # the next window opened
    store.db.commit()
    assert main._window_prices(store, W, TICKER) == (84787.45, 84810.906, 84862.28)
    store.db.execute("INSERT INTO settlements (ticker, strike, expiration_value) VALUES (?,?,?)",
                     (TICKER, 84787.45, 84862.29))
    store.db.commit()
    assert main._window_prices(store, W, TICKER)[2] == 84862.29, "the official value wins"


def test_the_price_line_names_the_instrument():
    lines = messages.target_lines("UP", 3912.4, 3915.0, asset="GOLD")
    assert lines[1].startswith("\U0001f4b2 GOLD at the signal $3,915.00")
    text = messages.allsignal_missed_message(asset="ETH", window_open=W, side="DOWN",
                                             signal_ask=0.7, limit=0.75, target=2400.0, ref=2398.0)
    assert "ETH at the signal $2,398.00" in text and "BTC at the signal" not in text
