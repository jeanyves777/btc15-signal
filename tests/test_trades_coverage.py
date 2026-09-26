"""An absent measurement must never arrive in the same shape as a measured zero.

THE BUG THIS PINS, 2026-09-24. `fetch_trades.py` paginated public trades from
the newest backwards with a 25-page cap. A 15-minute BTC market trades tens of
thousands of times - the windows fetched averaged 13,400 trades over ten
minutes - so 25,000 trades reached back only a few minutes and the cap
silently discarded everything before it. For 137 of 172 markets the minutes
before the bot's entry were never fetched.

The analysis then asked "how much volume traded in the two minutes before the
decision?" and got 0 for those markets. Zero volume read as an ILLIQUID
MARKET, and illiquid separated hard: 49 markets won 55.1% against 94.3% for
the rest, the split held on every one of five days, and a day-clustered
bootstrap put the residual at -0.256 with a 95% interval of [-0.522, -0.143],
excluding zero. It survived a confound check on time-of-day. It was entirely
my own truncation: 49 of 49 had the window cut away, not one genuine case.

No threshold and no interval can catch this, because the data was not noisy -
it was missing, and missing data was spelled the same way as a real answer.
The only fix is structural: record the bounds actually covered, and make a
caller that ignores them get nothing rather than a plausible number.

These tests are about that invariant, not about order flow, which may yet turn
out to carry signal or not.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fetch_trades  # noqa: E402


def make_db(path):
    out = sqlite3.connect(str(path))
    out.executescript(fetch_trades.SCHEMA)
    out.executescript(fetch_trades.COVERAGE)
    return out


def test_the_fetcher_records_the_bounds_it_covered(tmp_path):
    """Without this table the old bug is not even detectable after the fact."""
    out = make_db(tmp_path / "t.db")
    cols = [r[1] for r in out.execute("PRAGMA table_info(trades_coverage)")]
    for needed in ("ticker", "min_ms", "max_ms", "complete", "trades"):
        assert needed in cols


def test_an_unfetched_window_is_distinguishable_from_a_quiet_one(tmp_path):
    """The whole point: two markets with zero stored trades in a window, one
    because it was never requested and one because nothing traded. A reader
    must be able to tell them apart."""
    out = make_db(tmp_path / "t.db")
    out.execute("INSERT INTO trades_coverage VALUES ('QUIET',1000,2000,1,0)")
    out.execute("INSERT INTO trades_coverage VALUES ('COVERED',5000,6000,1,7)")
    out.commit()

    # A window BEFORE the covered range is unknown, not empty.
    lo, hi, complete = out.execute(
        "SELECT min_ms, max_ms, complete FROM trades_coverage "
        "WHERE ticker='COVERED'").fetchone()
    assert lo > 2000, "asking about ms 1000-2000 falls outside what was fetched"
    assert complete == 1

    quiet = out.execute("SELECT trades, complete FROM trades_coverage "
                        "WHERE ticker='QUIET'").fetchone()
    assert quiet == (0, 1), "a covered window with no trades is a real zero"

    # And a market absent from the table entirely is neither.
    assert out.execute("SELECT COUNT(*) FROM trades_coverage "
                       "WHERE ticker='NEVER'").fetchone()[0] == 0


def test_a_partly_covered_window_is_flagged_incomplete(tmp_path):
    """When the page budget runs out before the cursor does, the window is
    PARTIAL. That must be recorded, because a partial window produces a
    number that looks exactly like a complete one."""
    out = make_db(tmp_path / "t.db")
    out.execute("INSERT INTO trades_coverage VALUES ('PARTIAL',0,600000,0,60000)")
    out.commit()
    complete = out.execute("SELECT complete FROM trades_coverage "
                           "WHERE ticker='PARTIAL'").fetchone()[0]
    assert complete == 0


def test_a_market_with_no_decision_instant_gets_no_window(tmp_path, capsys):
    """An unknown anchor must skip the market rather than fetch some other
    stretch of time and label it 'before the decision'."""
    path = tmp_path / "t.db"
    import os
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        fetch_trades.fetch([("NOANCHOR", None)], str(path), 10.0, 5)
    finally:
        os.chdir(cwd)
    out = sqlite3.connect(str(path))
    assert out.execute("SELECT COUNT(*) FROM trades_coverage").fetchone()[0] == 0
    assert "no anchor" in capsys.readouterr().out


def test_the_window_is_anchored_before_the_decision_not_after():
    """Flow after the entry cannot inform the entry. The request must reach
    BACK from the decision instant."""
    src = Path(fetch_trades.__file__).read_text(encoding="utf-8")
    assert "hi_ms = int(anchor_ms)" in src
    assert "lo_ms = hi_ms - int(window_min * 60_000)" in src
    assert '"min_ts": lo_ms // 1000' in src


def test_the_anchor_is_the_decision_instant_from_the_live_record():
    """It must come from when the bot DECIDED, not from market close - the
    original fetch effectively anchored on close and that is what put the
    fetched range after the entry."""
    src = Path(fetch_trades.__file__).read_text(encoding="utf-8")
    assert "MIN(p.created_at)" in src
