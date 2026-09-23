"""Crossing-history coverage: what it can answer, and what it must not.

The gate asks "has BRTI been on the wrong side of the strike since we bought?"
It is a safety question, so an unanswerable one is never a pass - but it was
ALSO never distinguishable from a broken feed, and that is what turned a
routine one-second timing race into a permanent refusal.

Measured on the 26 refusals of 2026-09-23: the newest BRTI sample landed
between 1.9s BEHIND the entry instant and 1.7s AHEAD of it. BRTI publishes on
whole seconds and trails real time; the add is evaluated on the same poll the
entry fills. So the miss is sub-second and clears itself on the next poll.

Coverage now has to hold at BOTH ends and reach close enough to now:

* start at or before the entry, or an earlier crossing is invisible;
* reach the entry, or there is no sample in the interval at all;
* and not be stale, or we would answer "no crossing" for an interval we
  stopped watching. That third rule is NEW and makes the gate STRICTER - a
  stale series used to return a confident False.

Nothing here infers a crossing. None stays None.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.brti import features_from_series  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.reference_shadow import Crossing, ReferenceShadow  # noqa: E402

TARGET = 86_430.49
BASE = 1_790_196_576_000


def shadow(tmp_path, series, target=TARGET, stale_ms=15_000):
    ref = ReferenceShadow(Settings(
        reference_database_path=str(tmp_path / "ref.db"),
        reference_poll_seconds=0, cfb_api_key="",
        reference_stale_ms=stale_ms,
    ))
    ref._brti_series = sorted(series)
    ref._brti_event = "E"
    ref._brti_features = features_from_series(
        "E", ref._brti_series, target, series[-1][0]
    )
    return ref


def flat(n=60, value=86_400.0, start=BASE, step=1000):
    """A second-by-second series, all below the strike."""
    return [(start + i * step, value) for i in range(n)]


# --------------------------------------------------- the routine feed lag

def test_the_feed_lag_is_named_as_data_unavailable(tmp_path):
    """The measured case: newest sample 1.4s older than the entry."""
    ref = shadow(tmp_path, flat())
    entry = BASE + 59_000 + 1_400
    answer = ref.crossing_since(entry, "DOWN", entry + 100)
    assert answer.crossed is None
    assert "has not published a sample covering the entry" in answer.reason
    assert answer.short_by_ms == 1_400


def test_a_one_second_lag_clears_on_the_next_sample(tmp_path):
    """Why it must defer rather than refuse: it is answerable a second on."""
    entry = BASE + 59_000 + 1_400
    assert shadow(tmp_path, flat()).crossing_since(
        entry, "DOWN", entry + 100
    ).crossed is None
    caught_up = flat() + [(BASE + 61_000, 86_400.0)]
    later = shadow(tmp_path, caught_up).crossing_since(
        entry, "DOWN", BASE + 61_500
    )
    assert later.crossed is False
    assert later.reason == ""


# ------------------------------------------------- the timestamp boundary

def test_a_sample_exactly_at_the_entry_instant_answers(tmp_path):
    """`>=`, not `>`. An off-by-one here is a refusal on every whole second."""
    entry = BASE + 30_000
    answer = shadow(tmp_path, flat()).crossing_since(entry, "DOWN", entry + 100)
    assert answer.answered
    assert answer.crossed is False


def test_one_millisecond_past_the_last_sample_is_unanswered(tmp_path):
    ref = shadow(tmp_path, flat())
    last = BASE + 59_000
    assert ref.crossing_since(last, "DOWN", last + 10).crossed is False
    late = ref.crossing_since(last + 1, "DOWN", last + 10)
    assert late.crossed is None
    assert late.short_by_ms == 1


def test_history_starting_after_the_entry_cannot_rule_a_crossing_out(tmp_path):
    """A restart with a short buffer: the gap is BEFORE us, not after."""
    ref = shadow(tmp_path, flat())
    answer = ref.crossing_since(BASE - 60_000, "DOWN", BASE + 59_500)
    assert answer.crossed is None
    assert "starts after the entry" in answer.reason
    assert answer.short_by_ms == 60_000


# ------------------------------------------------------ missing samples

def test_no_series_at_all_is_named_differently_from_a_lag(tmp_path):
    """A feed that is down must not read like a feed that is 1s behind."""
    ref = shadow(tmp_path, flat())
    ref._brti_series = []
    answer = ref.crossing_since(BASE, "DOWN", BASE + 1000)
    assert answer.crossed is None
    assert "series unavailable" in answer.reason


def test_a_missing_target_is_unavailable_not_uncrossed(tmp_path):
    ref = shadow(tmp_path, flat())
    ref._brti_features = None
    assert ref.crossing_since(BASE, "DOWN", BASE + 1000).crossed is None


def test_a_gap_inside_the_window_still_answers_from_what_is_there(tmp_path):
    """Dropped seconds mid-series are not a coverage failure; the interval is
    still bounded at both ends. The gate reads what was published."""
    series = flat(30) + flat(30, start=BASE + 50_000)
    ref = shadow(tmp_path, series)
    answer = ref.crossing_since(BASE + 10_000, "DOWN", BASE + 79_500)
    assert answer.answered


# -------------------------------------------------------- staleness bound

def test_a_stale_series_no_longer_answers_no_crossing(tmp_path):
    """THE GATE GETS STRICTER. Covering the entry is not enough if we stopped
    watching: answering False here claims an interval we cannot see."""
    ref = shadow(tmp_path, flat(), stale_ms=15_000)
    entry = BASE + 30_000
    fresh = ref.crossing_since(entry, "DOWN", BASE + 60_000)
    assert fresh.crossed is False
    stale = ref.crossing_since(entry, "DOWN", BASE + 59_000 + 60_000)
    assert stale.crossed is None
    assert "stale" in stale.reason


def test_staleness_is_not_checked_when_no_clock_is_given(tmp_path):
    """Callers that only want the verdict keep the old behaviour."""
    assert shadow(tmp_path, flat()).crossed_since(BASE + 30_000, "DOWN") is False


# ------------------------------------------------- the gate still vetoes

def test_a_crossing_is_still_caught(tmp_path):
    points = flat()
    points[45] = (BASE + 45_000, TARGET + 100)  # above the strike, held DOWN
    answer = shadow(tmp_path, points).crossing_since(
        BASE + 30_000, "DOWN", BASE + 59_500
    )
    assert answer.crossed is True


def test_a_crossing_before_the_entry_is_not_counted_against_us(tmp_path):
    """The gate measures SINCE THE FILL. A crossing at +5s must not veto an
    entry made at +30s."""
    points = flat()
    points[5] = (BASE + 5_000, TARGET + 100)
    answer = shadow(tmp_path, points).crossing_since(
        BASE + 30_000, "DOWN", BASE + 59_500
    )
    assert answer.crossed is False


def test_an_up_position_is_crossed_by_a_dip_below(tmp_path):
    points = flat(60, value=TARGET + 50)
    points[40] = (BASE + 40_000, TARGET - 50)
    answer = shadow(tmp_path, points).crossing_since(
        BASE + 30_000, "UP", BASE + 59_500
    )
    assert answer.crossed is True


def test_unknown_is_never_turned_into_a_boolean(tmp_path):
    """Every unanswered path returns None, never a default."""
    ref = shadow(tmp_path, flat())
    cases = [
        ref.crossing_since(BASE + 59_001, "DOWN", BASE + 59_500),
        ref.crossing_since(BASE - 1, "DOWN", BASE + 59_500),
        ref.crossing_since(BASE + 30_000, "DOWN", BASE + 200_000),
    ]
    for answer in cases:
        assert answer.crossed is None
        assert answer.reason
        assert isinstance(answer, Crossing)


def test_the_answer_carries_no_reason_when_it_answered(tmp_path):
    answer = shadow(tmp_path, flat()).crossing_since(
        BASE + 30_000, "DOWN", BASE + 59_500
    )
    assert answer.answered
    assert answer.reason == ""
    assert answer.short_by_ms == 0


# ------------------------------------------- recorded cases from the live DB

RECORDED_LAGS_MS = [
    # (entry_ms - newest_brti_sample_ms) for the 2026-09-23 refusals, as
    # measured from btc15.db against runtime/settlement_reference.db.
    44, 1333, 45, 628, 276, 642, 955, 1021, 457, 773, 210, 1381,
    240, 1798, 565, 149, 362, 1240, 1338, 879, 1402, 424, 1030, 1915, 1365,
]


def test_every_recorded_refusal_is_a_sub_two_second_lag(tmp_path):
    """Not one of them was a feed outage. The largest miss was 1.9s."""
    assert max(RECORDED_LAGS_MS) < 2_000
    for lag in RECORDED_LAGS_MS:
        entry = BASE + 59_000 + lag
        answer = shadow(tmp_path, flat()).crossing_since(
            entry, "DOWN", entry + 50
        )
        assert answer.crossed is None
        assert answer.short_by_ms == lag
        assert "has not published" in answer.reason


def test_every_recorded_refusal_answers_once_the_feed_catches_up(tmp_path):
    """Which is what makes deferring correct and refusing wrong."""
    for lag in RECORDED_LAGS_MS:
        entry = BASE + 59_000 + lag
        caught_up = flat() + [(entry + 1_000, 86_400.0)]
        answer = shadow(tmp_path, caught_up).crossing_since(
            entry, "DOWN", entry + 1_100
        )
        assert answer.answered, lag
        assert answer.crossed is False, lag


def test_a_clock_in_the_wrong_units_does_not_silently_disable_staleness(tmp_path):
    """Passing SECONDS makes `now - last` hugely negative, so the staleness
    check never fires and a series of any age answers confidently. That is a
    weakening that raises nothing, so it is caught by its magnitude."""
    entry = BASE + 30_000
    answer = shadow(tmp_path, flat()).crossing_since(
        entry, "DOWN", int(time.time())
    )
    assert answer.crossed is None
    assert "clock" in answer.reason


def test_a_series_slightly_ahead_of_the_clock_still_answers(tmp_path):
    """BRTI timestamps run a second or two ahead of ours routinely; that is
    drift, not a units error, and must not refuse."""
    entry = BASE + 30_000
    answer = shadow(tmp_path, flat()).crossing_since(
        entry, "DOWN", BASE + 57_000
    )
    assert answer.crossed is False
