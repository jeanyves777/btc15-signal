"""The midnight count gap (operator, 2026-10-01: "address the midnight count gap").

A 23:45 position held to settlement settles ~00:00:06: after the old day's last
refresh, and excluded from the new day (its result is already in the new opening).
09-30's 23:45 window lost -5.81 and the day read +8.08 instead of +2.26.
"""

import inspect
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.daily_profit import DailyProfitGuard  # noqa: E402

NY = ZoneInfo("America/New_York")
MIDNIGHT = int(datetime(2026, 10, 1, tzinfo=NY).timestamp() * 1000)   # 10-01 00:00 ET
DAY_START = int(datetime(2026, 9, 30, tzinfo=NY).timestamp() * 1000)
EVENING = "KXBTC15M-26SEP302300-00"     # 22:45-23:00 on 09-30
LAST = "KXBTC15M-26OCT010000-00"        # 23:45-00:00: the day's last market
NEXT = "KXBTC15M-26OCT010015-15"        # 00:00-00:15: the new day's first


def guard(tmp_path, stored=8.08):
    g = DailyProfitGuard(tmp_path / "g.db", "primary", "You", SimpleNamespace(), 0.08)
    with g.connect() as db:
        db.execute("INSERT INTO profit_days (account,day,label,opening,captured_ms,start_ms,"
                   "basis,target,pnl,peak,updated_ms) VALUES ('primary','2026-09-30','You',"
                   "106.05,?,?,'day opening',8.484,?,?,?)",
                   (DAY_START, DAY_START, stored, stored, MIDNIGHT - 10_000))
    return g


EVENTS = [
    (MIDNIGHT - 3_600_000, 8.08, EVENING),     # the day as it read before midnight
    (MIDNIGHT + 6_000, -5.81, LAST),           # the 23:45 market settling at 00:00:06
    (MIDNIGHT + 1_200_000, 1.00, NEXT),        # the new day's own market - not the old day's
]


def test_the_days_last_market_is_settled_into_its_own_day(tmp_path):
    g = guard(tmp_path)
    got = g.close_out(EVENTS, MIDNIGHT, MIDNIGHT + 1_800_000)
    assert got == pytest.approx(2.27)
    row = g.connect().execute("SELECT pnl FROM profit_days WHERE day='2026-09-30'").fetchone()
    assert row["pnl"] == pytest.approx(2.27), "09-30 reads +2.27, not +8.08"


def test_it_is_idempotent_and_ignores_events_after_now(tmp_path):
    g = guard(tmp_path)
    for _ in range(3):
        g.close_out(EVENTS, MIDNIGHT, MIDNIGHT + 60_000)
    assert g.close_out(EVENTS, MIDNIGHT, MIDNIGHT + 60_000) == pytest.approx(2.27)
    assert g.close_out(EVENTS, MIDNIGHT, MIDNIGHT + 1_000) == pytest.approx(8.08), \
        "before the settlement lands, nothing changes"


def test_a_day_with_nothing_held_over_midnight_is_unchanged(tmp_path):
    g = guard(tmp_path)
    assert g.close_out(EVENTS[:1] + EVENTS[2:], MIDNIGHT, MIDNIGHT + 60_000) == pytest.approx(8.08)
    row = g.connect().execute("SELECT pnl FROM profit_days WHERE day='2026-09-30'").fetchone()
    assert row["pnl"] == pytest.approx(8.08)


def test_no_previous_row_is_a_no_op(tmp_path):
    g = DailyProfitGuard(tmp_path / "g.db", "m3", "Affoue", SimpleNamespace(), 0.15)
    assert g.close_out(EVENTS, MIDNIGHT, MIDNIGHT + 60_000) is None


def test_refresh_closes_out_all_through_the_new_day():
    """Not just 00:00-00:30: an outage across that half hour left the 23:45
    settlement in neither day again (review 2026-10-01)."""
    src = " ".join(inspect.getsource(DailyProfitGuard.refresh).split())
    assert "if now - midnight < self.CLOSE_OUT_MS: self.close_out(events, midnight, now)" in src
    assert DailyProfitGuard.CLOSE_OUT_MS == 24 * 3_600_000


def test_a_close_out_hours_late_still_settles_the_day(tmp_path):
    g = guard(tmp_path)
    assert g.close_out(EVENTS, MIDNIGHT, MIDNIGHT + 3 * 3_600_000) == pytest.approx(2.27)
