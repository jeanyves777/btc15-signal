"""Session breakdown and the close report.

Two properties matter more than the formatting:

* the breakdown must SUM to the day's total it sits under - a breakdown that
  does not add up invites the reader to trust neither number
* a close must be reported exactly once, even when a slow poll or a restart
  steps over the boundary, and even when the gap spans more than one close
"""

import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.capital import ny_day, ny_day_start_ms  # noqa: E402
from btc15_signal.sessions import (  # noqa: E402
    SESSIONS,
    breakdown,
    closes_between,
    one_line,
    session_of,
)
from btc15_signal.store import Store  # noqa: E402

NOW = int(time.time() * 1000)


def at(day: str, hour: int) -> int:
    return int(
        dt.datetime.fromisoformat(f"{day}T{hour:02d}:30:00+00:00").timestamp() * 1000
    )


def test_labels_match_the_archives_own_definition():
    """`features._session` is what every measurement in FINDINGS used. If
    these drift, a live report and a backtest stop meaning the same thing."""
    from btc15_signal.features import _session

    for hour in range(24):
        ms = at("2026-09-22", hour)
        assert session_of(ms) == _session(ms), hour


def test_every_hour_belongs_to_exactly_one_session():
    seen = {}
    for name, start, end in SESSIONS:
        for hour in range(start, end):
            assert hour not in seen, f"hour {hour} in two sessions"
            seen[hour] = name
    assert len(seen) == 24


# --------------------------------------------------------------- the sum

def test_the_breakdown_sums_to_the_day(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    start = ny_day_start_ms(NOW)
    for offset, pnl in ((0, 0.5), (3 * 3_600_000, -0.2), (10 * 3_600_000, 0.8),
                        (18 * 3_600_000, -0.1)):
        store.record_realised(
            f"KX-{offset}", start + offset, pnl, pnl > 0, "exchange", NOW,
            realised_ms=start + offset,
        )
    snapshot = store.money_snapshot(NOW)
    assert round(sum(s.dollars for s in snapshot.sessions), 6) == round(
        snapshot.realised, 6
    )
    assert sum(s.markets for s in snapshot.sessions) == snapshot.markets


def test_a_new_york_day_holds_a_full_seven_hours_of_asia():
    """The reason a breakdown on the NY day is complete rather than clipped:
    hours 04-06 at the start plus 00-03 at the end is the same seven hours the
    Asian session has. Discontiguous, not missing."""
    start = ny_day_start_ms(at("2026-09-22", 12))
    hours = [
        session_of(start + h * 3_600_000) for h in range(24)
    ]
    assert hours.count("asia") == 7
    assert hours.count("europe") == 6
    assert hours.count("us") == 8
    assert hours.count("late-us") == 3


def test_empty_sessions_are_dropped_not_shown_as_zero():
    results = breakdown([(at("2026-09-22", 9), 0.4)])
    assert [r.name for r in results] == ["europe"]
    assert "Asia" not in one_line(results)


# ------------------------------------------------------- the close trigger

def test_a_close_is_detected_when_the_poll_steps_over_it():
    before = at("2026-09-22", 12)          # 12:30, europe still open
    after = int(
        dt.datetime.fromisoformat("2026-09-22T13:00:10+00:00").timestamp() * 1000
    )
    assert closes_between(before, after) == ["europe"]


def test_no_close_inside_a_session():
    assert closes_between(at("2026-09-22", 9), at("2026-09-22", 10)) == []


def test_a_long_gap_reports_every_close_it_spanned():
    """A restart or a stall must not swallow a session report."""
    before = at("2026-09-22", 6)
    after = at("2026-09-22", 22)
    assert closes_between(before, after) == ["asia", "europe", "us"]


def test_the_first_poll_after_start_reports_nothing():
    """`previous_ms` of 0 means we have no idea what was stepped over, and
    inventing a close would send a report for a session we never watched."""
    assert closes_between(0, NOW) == []


def test_a_close_is_reported_once_per_day(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    day = ny_day(NOW)
    assert not store.session_reported(day, "europe")
    store.mark_session_reported(day, "europe", NOW)
    assert store.session_reported(day, "europe")
    assert not store.session_reported(day, "us"), "other sessions unaffected"
    assert not store.session_reported("2026-01-01", "europe"), "other days too"


# ------------------------------------------------------------- the message

def test_the_close_report_carries_the_session_and_the_day(tmp_path):
    from btc15_signal import messages

    store = Store(str(tmp_path / "s.db"))
    start = ny_day_start_ms(NOW)
    # Offsets run from the NY day start (04:00 UTC), so +4h is 08:00 UTC -
    # inside the European session. +9h would be 13:00 and land in US.
    europe_ms = start + 4 * 3_600_000
    store.record_realised(
        "KX-EU", europe_ms, 0.40, True, "exchange", NOW, realised_ms=europe_ms,
    )
    snapshot = store.money_snapshot(NOW)
    session = breakdown(store.session_rows_for("europe", NOW))[0]
    text = messages.session_close(
        session=session, day_snapshot=snapshot, ny_day=ny_day(NOW)
    )
    assert "Europe session closed" in text
    assert "Session:" in text
    assert "Today (New York)" in text, "the day is the context, always shown"
    assert "Live" in text, "and the running record above it"


def test_a_quiet_session_still_reports(tmp_path):
    """Silence is a result. A session with no trades that sends nothing is
    indistinguishable from a reporter that broke."""
    from btc15_signal import messages
    from btc15_signal.sessions import SessionResult

    store = Store(str(tmp_path / "s.db"))
    text = messages.session_close(
        session=SessionResult("asia", 0, 0, 0.0),
        day_snapshot=store.money_snapshot(NOW), ny_day=ny_day(NOW),
    )
    assert "Asia session closed" in text
    assert "0 closed" in text
