"""The live total must never un-bank money that is already in the account.

The exact sequence from 2026-09-22, KXBTC15M-26SEP221245-45:

    12:41  cash-out message      Live today: +$4.49
    12:45  settlement recap      Live today: +$3.95      <- the $0.55 vanished
    12:49  next signal           Live today: +$4.51      <- and came back

No money moved. The account was flat the whole time. The cause is that the
headline added two different 60-second snapshots: `settlements`, which had not
yet been told about the sale, and `open_mark`, which was still marking the
position that had just been sold. For one sync cycle the same $0.55 was both
pending and open; at the next cycle it was neither.

These tests pin the fix: a realised figure is written the moment it is real,
is read by every message from one place, and never goes backwards.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.capital import ny_day_start_ms
from btc15_signal.store import Store  # noqa: E402

DAY = 86_400_000
TICKER = "KXBTC15M-26SEP221245-45"

# The ledger buckets by the CURRENT UTC day, so the fixtures have to live
# in it. A hard-coded timestamp silently lands in the wrong day and every
# total reads zero.
NOW = int(time.time() * 1000)


def window_today(now_ms: int) -> int:
    """An hour into the CURRENT New York accounting day.

    The day moved from UTC to New York so the whole account is kept on the
    exchange's own boundary; a fixture built on UTC arithmetic lands outside
    it for four or five hours a day depending on the season.
    """
    return ny_day_start_ms(now_ms) + 3_600_000


def test_cash_out_is_banked_before_the_settlement_arrives(tmp_path):
    now = NOW
    window = window_today(now)
    store = Store(str(tmp_path / "t.db"))

    store.record_realised(TICKER, window, 0.5515, True, "cash_out", now)
    markets, winners, dollars = store.ledger_today(now)
    assert (markets, winners) == (1, 1)
    assert dollars == 0.5515

    # The settlement lands a minute later carrying the same money. It must not
    # be added a second time, and must not reset anything.
    store.record_realised(TICKER, window, 0.5515, True, "exchange", now + 60_000)
    assert store.ledger_today(now)[2] == 0.5515
    assert store.ledger_today(now)[0] == 1


def test_a_banked_profit_is_never_dropped_by_a_later_read(tmp_path):
    """The settlement recap must read the same number the cash-out reported."""
    now = NOW
    window = window_today(now)
    store = Store(str(tmp_path / "t.db"))
    store.record_realised(TICKER, window, 0.5515, True, "cash_out", now)

    before = store.ledger_today(now)[2]
    # Four minutes later the recap renders. Nothing has been synced yet.
    after = store.ledger_today(now + 240_000)[2]
    assert after == before == 0.5515


def test_open_mark_excludes_a_position_already_banked(tmp_path):
    """The double count itself: sold, banked, but still in the stale mark."""
    now = NOW
    window = window_today(now)
    store = Store(str(tmp_path / "t.db"))

    # The 60-second mark still carries the position we sold seconds ago.
    store.set_setting_text(
        "open_mark_detail", json.dumps({TICKER: 1.96}), now
    )
    store.set_setting("open_mark", 1.96, now)
    assert store.open_exposure() == 1.96  # nothing banked yet

    store.record_realised(TICKER, window, 0.5515, True, "cash_out", now)
    assert store.open_exposure() == 0.0, "a banked position must leave the mark"

    _, _, dollars = store.realised_record()
    assert dollars == 0.5515, "banked once, not banked plus marked"


def test_a_genuinely_open_position_still_counts(tmp_path):
    """The exclusion must not understate: an unbanked position is real."""
    now = NOW
    store = Store(str(tmp_path / "t.db"))
    store.set_setting_text(
        "open_mark_detail",
        json.dumps({TICKER: 0.55, "KXBTC15M-26SEP221300-00": 0.20}),
        now,
    )
    store.record_realised(TICKER, window_today(now), 0.55, True, "cash_out", now)
    assert abs(store.open_exposure() - 0.20) < 1e-9


def test_the_exchange_may_revise_a_locally_banked_figure(tmp_path):
    """The broker is still the authority, but the change is counted."""
    now = NOW
    window = window_today(now)
    store = Store(str(tmp_path / "t.db"))
    store.record_realised(TICKER, window, 0.5515, True, "cash_out", now)
    store.record_realised(TICKER, window, 0.5480, True, "exchange", now + 60_000)

    row = store.db.execute(
        "SELECT pnl, source, revisions FROM daily_ledger WHERE ticker=?", (TICKER,)
    ).fetchone()
    assert row[0] == 0.5480
    assert row[1] == "exchange"
    assert row[2] == 1, "a silent revision is the failure mode, so count it"


def test_a_local_figure_never_overrides_the_exchange(tmp_path):
    now = NOW
    window = window_today(now)
    store = Store(str(tmp_path / "t.db"))
    store.record_realised(TICKER, window, 0.5480, True, "exchange", now)
    store.record_realised(TICKER, window, 9.9999, True, "cash_out", now + 1_000)
    assert store.ledger_today(now)[2] == 0.5480


def test_yesterday_is_not_counted_in_today(tmp_path):
    now = NOW
    store = Store(str(tmp_path / "t.db"))
    yesterday = ny_day_start_ms(now) - 3_600_000
    store.record_realised("OLD", yesterday, 5.0, True, "exchange", now)
    store.record_realised(TICKER, window_today(now), 0.55, True, "cash_out", now)
    markets, _, dollars = store.ledger_today(now)
    assert markets == 1
    assert dollars == 0.55


def test_sync_from_settlements_populates_the_ledger(tmp_path):
    now = NOW
    window = window_today(now)
    store = Store(str(tmp_path / "t.db"))
    store.db.execute(
        "INSERT INTO settlements (ticker, market_result, pnl, settled_ms, "
        "synced_at, window_ms) VALUES (?,?,?,?,?,?)",
        (TICKER, "yes", 0.5515, now, now, window),
    )
    store.db.commit()
    store.sync_ledger_from_settlements(now)
    assert store.ledger_today(now)[2] == 0.5515
    assert store.db.execute(
        "SELECT source FROM daily_ledger WHERE ticker=?", (TICKER,)
    ).fetchone()[0] == "exchange"


def test_the_full_reported_sequence_no_longer_dips(tmp_path):
    """End to end: the three messages must not disagree about the same money."""
    now = NOW
    window = window_today(now)
    store = Store(str(tmp_path / "t.db"))

    # Earlier settled markets today.
    store.record_realised("A", window, 2.00, True, "exchange", now)
    store.record_realised("B", window, 1.94, True, "exchange", now)

    # The position is open and marked.
    store.set_setting_text("open_mark_detail", json.dumps({TICKER: 1.96}), now)

    # 12:41 - the cash-out fires and banks its profit.
    store.record_realised(TICKER, window, 0.55, True, "cash_out", now)
    at_cash_out = store.realised_record()[2]

    # 12:45 - the settlement recap, before any sync.
    at_recap = store.realised_record()[2]

    # 12:49 - the broker sync has landed and the mark has caught up.
    store.db.execute(
        "INSERT INTO settlements (ticker, market_result, pnl, settled_ms, "
        "synced_at, window_ms) VALUES (?,?,?,?,?,?)",
        (TICKER, "yes", 0.55, now, now, window),
    )
    store.db.commit()
    store.sync_ledger_from_settlements(now + 240_000)
    store.set_setting_text("open_mark_detail", json.dumps({}), now + 240_000)
    at_next_signal = store.realised_record()[2]

    assert at_cash_out == at_recap == at_next_signal, (
        f"the headline moved without money moving: {at_cash_out} -> "
        f"{at_recap} -> {at_next_signal}"
    )
    assert abs(at_recap - 4.49) < 1e-9
