"""The daily loss limit counts every market on its own day, graded on its own side.

Two defects in the floor, found 2026-09-28 and fixed the same day (FINDINGS 108,
operator: "address all 4").

A. THE CLOCK. `market_open_ms` read the ticker's time - the market's CLOSE on
   the New York clock - as UTC, so every stored `window_ms` sat 3h45m early and
   markets closing 00:15-03:45 ET fell out of their own day: 17 markets and
   -$20.14 were missing from the account-wide floor on 09-28 (-2.01 counted,
   -22.15 real). The ticker agrees with Kalshi's own times on 4,305 of 4,305
   stored rows once read in America/New_York.

B. THE SIDE. The local rebuild graded a trade by the signal's first side, so a
   trade placed after the signal flipped booked the opposite result:
   KXSOL15M-26SEP271615-15 held DOWN and lost $3.32; the floor booked +$0.68.
"""

import datetime as dt
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from btc15_signal.execution import KalshiExecutionClient  # noqa: E402
from btc15_signal.store import TRADE_WON_SQL, Store  # noqa: E402
from test_exchange_pnl import todays_ticker  # noqa: E402

open_ms = KalshiExecutionClient.market_open_ms


def ms(*args, tz=UTC):
    return int(datetime(*args, tzinfo=tz).timestamp() * 1000)


def now_ms():
    return int(dt.datetime.now(UTC).timestamp() * 1000)


# ------------------------------------------------------------------ A: clock

def test_the_ticker_is_the_new_york_close():
    # 04:45 ET close (EDT) -> 04:30 ET open -> 08:30 UTC
    assert open_ms("KXBTC15M-26SEP220445-45") == ms(2026, 9, 22, 8, 30)
    # hourly ladder: 07:00 ET close -> 06:00 ET open -> 10:00 UTC
    assert open_ms("KXBTCD-26SEP2207-T80099.99") == ms(2026, 9, 22, 10, 0)
    # EST after 11-01: 00:15 close -> 00:00 open -> 05:00 UTC
    assert open_ms("KXBTC15M-26NOV020015-15") == ms(2026, 11, 2, 5, 0)
    # the gold ticker the review checked against the public API
    assert open_ms("KXGOLD15M-26SEP280930-30") == ms(2026, 9, 28, 13, 15)


def test_a_length_it_does_not_know_is_none_not_a_guess():
    assert open_ms("KXFOO-26SEP221030-30") is None
    assert open_ms("KXMVECROSSCATEGORY-S2026ABC-DEF") is None


def settlement(ticker, pnl_loss=True):
    return {
        "ticker": ticker, "market_result": "no" if pnl_loss else "yes",
        "yes_count_fp": "1.00", "yes_total_cost_dollars": "0.790000",
        "no_count_fp": "0.00", "no_total_cost_dollars": "0.000000",
        "revenue": 0 if pnl_loss else 100, "value": 0, "fee_cost": "0.010000",
    }


def test_a_market_closing_after_midnight_counts_on_its_own_day(tmp_path):
    """The case that hid -$20.14 on 09-28: closes 00:15 ET, belongs to today."""
    store = Store(str(tmp_path / "s.db"))
    now = now_ms()
    store.record_settlements([
        settlement(todays_ticker("0015-15")),     # opened 00:00 ET today
        settlement(todays_ticker("0345-45")),     # opened 03:30 ET today
        settlement(todays_ticker("0000-00")),     # opened 23:45 ET YESTERDAY
    ], now)
    store.sync_ledger_from_settlements(now)
    day = store.day_start_ms(now)
    markets, _wins, dollars = store.exchange_record(day)
    assert markets == 2 and round(dollars, 2) == -1.60
    assert store.ledger_today(now)[0] == 2


def old_utc_parse(ticker):
    """What `market_open_ms` returned before the fix, to build old rows."""
    import re

    y, mon, d, h, m = re.match(
        r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})?", ticker).groups()
    months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
              "OCT", "NOV", "DEC")
    return ms(2000 + int(y), months.index(mon) + 1, int(d), int(h), int(m or 0))


def test_rows_written_on_the_old_clock_are_moved_once_and_only_those(tmp_path):
    path = str(tmp_path / "old.db")
    store = Store(path)
    k1, k2 = "KXBTC15M-26SEP280115-15", "KXETH15M-26SEP271930-30"
    combo, gas = "KXMVECROSSCATEGORY-S2026ABC-DEF", "KXAAAGASD-26SEP28-3.5"
    wrong = old_utc_parse(k1)
    true_k2 = open_ms(k2)
    db = store.db
    db.execute("INSERT INTO settlements (ticker, market_result, pnl, settled_ms, "
               "window_ms) VALUES (?, 'no', -1.0, ?, ?)", (k1, wrong + 1, wrong))
    db.execute("INSERT INTO settlements (ticker, market_result, pnl, settled_ms, "
               "window_ms) VALUES (?, 'no', -1.0, 5, 12345)", (combo,))
    db.execute("INSERT INTO settlements (ticker, market_result, pnl, settled_ms, "
               "window_ms) VALUES (?, 'no', -1.0, 5, 777)", (gas,))
    db.execute("INSERT INTO fills (fill_id, ticker, filled_ms, window_ms) "
               "VALUES ('f1', ?, 1, ?)", (k1, wrong))
    db.execute("INSERT INTO daily_ledger (ticker, window_ms, pnl, won, source, "
               "first_ms, updated_ms, revisions) VALUES (?, ?, -1.0, 0, "
               "'exchange', 1, 1, 0)", (k1, wrong))
    db.execute("INSERT INTO daily_ledger (ticker, window_ms, pnl, won, source, "
               "first_ms, updated_ms, revisions) VALUES (?, ?, 0.2, 1, "
               "'cash_out', 1, 1, 0)", (k2, true_k2))
    db.execute("INSERT INTO realised_events (event_id, ticker, realised_ms, "
               "amount, source, window_ms, recorded_ms) VALUES ('e1', ?, 1, "
               "-1.0, 'exchange', ?, 1)", (k1, wrong))
    db.commit()
    db.close()

    reopened = Store(path)                     # the migration runs on open
    q = reopened.db.execute
    right = open_ms(k1)
    assert right - wrong == 3 * 3_600_000 + 45 * 60_000
    for table in ("settlements", "fills", "daily_ledger", "realised_events"):
        got = q(f"SELECT window_ms FROM {table} WHERE ticker=?", (k1,)).fetchone()[0]
        assert got == right, table
    assert q("SELECT window_ms FROM daily_ledger WHERE ticker=?", (k2,)).fetchone()[0] == true_k2
    assert q("SELECT window_ms FROM settlements WHERE ticker=?", (combo,)).fetchone()[0] == 12345
    assert q("SELECT window_ms FROM settlements WHERE ticker=?", (gas,)).fetchone()[0] == 777
    assert reopened._migrate_window_clock() == 0, "idempotent"


def test_the_old_day_boundary_carry_is_retired():
    """With the true clock it would charge the previous evening's losses to
    each new day a second time (-5.57 on 09-21 in the replay)."""
    import inspect

    from btc15_signal import store as store_mod

    assert Store.migrate_day_boundary(None, now_ms()) == 0.0
    assert Store.day_boundary_carry(None, now_ms()) == 0.0
    assert "RETIRED" in inspect.getsource(store_mod.Store.migrate_day_boundary)


# --------------------------------------------------------------- B: the side

def flipped(store, *, signal_side, signal_won, trade_side, count, fill, fee,
            ticker, created):
    window = created - (created % 900_000)
    store.record((window, created, 81_000.0, 80_900.0, signal_side, 8, 0.9,
                  ticker, fill, 1))
    store.db.execute("UPDATE predictions SET won=? WHERE window_open=?",
                     (signal_won, window))
    p = store.create_proposal("primary", window, ticker, trade_side, fill, 0.0,
                              count, window + 900_000, window + 900_000, created)
    store.db.execute("UPDATE trade_proposals SET status='filled', fill_price=?, "
                     "fee_paid=? WHERE id=?", (fill, fee, p.id))
    store.db.commit()
    return window


def test_a_flipped_trade_that_lost_is_booked_as_a_loss(tmp_path):
    """SOL 09-27: the signal said UP and UP won; the trade held DOWN x4 at
    0.82 and lost. The floor's local rebuild booked it as a win."""
    store = Store(str(tmp_path / "sol.db"))
    now = now_ms()
    flipped(store, signal_side="UP", signal_won=1, trade_side="DOWN", count=4,
            fill=0.82, fee=0.0413, ticker="KXSOL15M-26SEP271615-15",
            created=store.day_start_ms(now) + 60_000)
    realised = store.auto_state(now)[3]
    assert round(realised, 4) == round(-4 * 0.82 - 0.0413, 4)


def test_the_brokers_result_decides_once_it_has_synced(tmp_path):
    """BTC 09-23: signal DOWN lost (so UP won); the trade held UP x2 at
    0.932 - a WIN the rebuild booked as -1.87."""
    store = Store(str(tmp_path / "btc.db"))
    now = now_ms()
    ticker = "KXBTC15M-26SEP231215-15"
    flipped(store, signal_side="DOWN", signal_won=0, trade_side="UP", count=2,
            fill=0.932, fee=0.0092, ticker=ticker,
            created=store.day_start_ms(now) + 60_000)
    expected = round(2 * (1 - 0.932) - 0.0092, 4)

    def by_hour():
        return round(sum(b["real"] for k, b in store.by_session().items()
                         if len(k) == 5 and k.endswith(":00")), 4)

    # Before any settlement syncs: re-expressed from the signal, every reader.
    # The floor takes the MORE negative of local and exchange, so a win reads
    # 0.00 there - where the side-blind rebuild read -1.87.
    assert store.auto_state(now)[3] == 0.0
    assert round(store.ledger()[-1]["pnl"], 4) == expected
    assert round(store.realised_record()[2], 4) == expected
    assert by_hour() == expected

    # The broker's result on the trade's own ticker decides once it lands,
    # even against a signal record that says otherwise.
    store.record_settlements([settlement(ticker, pnl_loss=False)], now)
    store.db.execute("UPDATE predictions SET won=1")      # wrong for the UP trade
    store.db.commit()
    assert round(store.ledger()[-1]["pnl"], 4) == expected
    assert by_hour() == expected
    assert store.auto_state(now)[3] == 0.0


def test_a_combo_is_never_graded_on_its_trigger_leg():
    assert TRADE_WON_SQL.startswith(
        "CASE WHEN t.strategy = 'combo_recovery' THEN NULL")


# ------------------------------------------ C: each instrument's own floor

def test_one_instruments_losses_do_not_stop_another(tmp_path):
    """Operator, 2026-09-28: gold "must be live". The floor read the whole
    account, so the morning's ETH/SOL/BTC losses would have stopped gold at
    its $14 while gold was flat. Each store now counts its own series."""
    from types import SimpleNamespace

    now = now_ms()
    gold = Store(str(tmp_path / "gold.db"))
    gold.configure_instrument(SimpleNamespace(kalshi_series="KXGOLD15M"))
    btc_loss = {**settlement(todays_ticker("0015-15")),
                "yes_total_cost_dollars": "13.000000", "yes_count_fp": "13.00"}
    eth_loss = {**btc_loss, "ticker": todays_ticker("0030-30").replace(
        "KXBTC15M", "KXETH15M")}
    gold_small = {**settlement(todays_ticker("0915-15").replace(
        "KXBTC15M", "KXGOLD15M"))}
    gold.record_settlements([btc_loss, eth_loss, gold_small], now)
    assert gold.exchange_record(gold.day_start_ms(now))[2] < -26, "account-wide"
    realised = gold.auto_state(now)[3]
    assert round(realised, 2) == -0.80, "only gold's own market counts"

    btc = Store(str(tmp_path / "btc.db"))
    btc.configure_instrument(SimpleNamespace(kalshi_series="KXBTC15M"))
    btc.record_settlements([btc_loss, eth_loss, gold_small], now)
    assert round(btc.auto_state(now)[3], 2) == -13.01


def test_an_unbound_store_still_reads_the_whole_account(tmp_path):
    now = now_ms()
    store = Store(str(tmp_path / "any.db"))
    store.record_settlements([settlement(todays_ticker("0015-15"))], now)
    assert round(store.auto_state(now)[3], 2) == -0.80
