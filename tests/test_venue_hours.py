"""Gold and silver keep New York hours; the service must not poll through them.

WHAT HAPPENS WITHOUT THIS. Gold and silver close at the New York close and
reopen at the New York open, so they are shut every weekend for about two days.
On 2026-09-25 both went quiet at 21:00 UTC and the next market Kalshi listed
opened 2026-09-28T02:45:00Z - 53 hours later. At a ten-second poll that is
roughly 19,000 requests and an equal number of reference fetches, recording
nothing, because the underlying metal is not trading either.

THE FAILURE THESE TESTS GUARD, which is worse than the waste: calling an OUTAGE
a closure. On 2026-09-24 Kalshi listed nothing for two hours while the service
was healthy, and the operator's only symptom was Telegram going quiet. If a
closure check swallows that, the alert built for it never fires again. So the
closure has to be EVIDENCED by the venue's own schedule, and anything unknown
stays an outage.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.main import VENUE_SCHEDULE, _venue_closed_until  # noqa: E402

HOUR = 3_600_000
NOW = 1_800_000_000_000


class FakeKalshi:
    """Stands in for the exchange. `next_open` is what it will answer."""

    def __init__(self, next_open=None, fail=False):
        self.next_open = next_open
        self.fail = fail
        self.calls = 0

    async def next_open_ms(self, now_ms):
        self.calls += 1
        if self.fail:
            raise RuntimeError("kalshi unreachable")
        return self.next_open


@pytest.fixture(autouse=True)
def _clean():
    VENUE_SCHEDULE.clear()
    yield
    VENUE_SCHEDULE.clear()


def sessions(**over):
    """A metals instrument: one that declares it observes sessions."""
    return Settings(venue_has_sessions=True, **over)


def closed(kalshi, gap_s, settings=None):
    """Sync wrapper: this suite drives async code with `asyncio.run` rather
    than pytest-asyncio, which is not a dependency here."""
    return asyncio.run(
        _venue_closed_until(kalshi, settings or sessions(), NOW, gap_s))


# ------------------------------------------------- the weekend it exists for

def test_a_weekend_closure_is_recognised():
    """The real case: 53 hours until the next gold market."""
    k = FakeKalshi(next_open=NOW + 53 * HOUR)
    assert closed(k, gap_s=1200) == NOW + 53 * HOUR


def test_the_schedule_is_asked_once_not_every_poll():
    """Asking every poll would spend the requests the closure saves."""
    k = FakeKalshi(next_open=NOW + 53 * HOUR)
    for _ in range(20):
        assert closed(k, gap_s=1200)
    assert k.calls == 1, f"asked Kalshi {k.calls} times"


# ------------------------------------------- an outage is NOT a closure

def test_a_two_hour_outage_is_not_treated_as_a_closure():
    """2026-09-24: Kalshi listed nothing for two hours while healthy. With no
    next market scheduled, the answer must be "not closed" so the outage alert
    still fires."""
    assert closed(FakeKalshi(next_open=None), gap_s=7200) is None


def test_a_failed_lookup_is_not_treated_as_a_closure():
    """Unknown is not closed. The conservative direction is to stay noisy."""
    k = FakeKalshi(fail=True)
    assert closed(k, gap_s=7200) is None


def test_a_next_market_opening_soon_is_a_boundary_not_a_closure():
    """Kalshi creates markets in daily batches, so on a 24/7 series the next
    UNOPENED market can be a day out while trading is continuous. A market
    opening within the threshold is the ordinary boundary."""
    assert closed(FakeKalshi(next_open=NOW + 300_000), gap_s=1200) is None


# ------------------------------------------ it must not touch a 24/7 series

def test_a_short_gap_never_asks_at_all():
    """A window flip is seconds. BTC, ETH and SOL must never reach the lookup,
    so the closure path cannot slow a 24/7 instrument or cost it a request."""
    k = FakeKalshi(next_open=NOW + 53 * HOUR)
    assert closed(k, gap_s=5) is None
    assert closed(k, gap_s=120) is None
    assert k.calls == 0


def test_a_24_7_instrument_never_enters_the_closed_path():
    """THE REGRESSION THIS PREVENTS. BTC and SOL list their next UNOPENED market
    hours ahead while trading normally, because Kalshi batches markets daily and
    an `unopened` listing excludes the ones already open. Inferring a closure
    from that would back BTC off mid-outage and suppress the alert built for the
    two-hour gap of 2026-09-24. The default is off, so it cannot happen."""
    assert Settings().venue_has_sessions is False
    k = FakeKalshi(next_open=NOW + 5 * HOUR)
    assert closed(k, gap_s=7200, settings=Settings()) is None
    assert k.calls == 0, "a 24/7 instrument must not even ask"


def test_only_the_declaring_instrument_backs_off():
    """Same exchange answer, opposite handling - the difference is the
    declaration, not the listing."""
    far = NOW + 48 * HOUR
    assert closed(FakeKalshi(next_open=far), gap_s=1200,
                  settings=sessions()) == far
    assert closed(FakeKalshi(next_open=far), gap_s=1200,
                  settings=Settings()) is None


def test_the_metals_launchers_declare_session_hours():
    root = Path(__file__).resolve().parents[1]
    for name in ("gold", "silver"):
        text = (root / "scripts" / f"run_{name}.ps1").read_text(
            encoding="utf-8")
        assert 'VENUE_HAS_SESSIONS      = "true"' in text, name
    for name in ("eth", "sol"):
        script = root / "scripts" / f"run_{name}.ps1"
        if script.exists():
            assert "VENUE_HAS_SESSIONS" not in script.read_text(
                encoding="utf-8"), f"{name} trades 24/7"


def test_the_threshold_comes_from_settings_not_a_constant():
    s = sessions()
    assert s.venue_closed_after_s == 600
    assert s.venue_closed_gap_s == 1800
    assert s.venue_closed_poll_seconds == 600
    k = FakeKalshi(next_open=NOW + 53 * HOUR)
    assert closed(k, gap_s=s.venue_closed_after_s - 1, settings=s) is None
    assert k.calls == 0


# ------------------------------------------------------- what the loop does

def test_the_closed_branch_skips_the_reference_fetch_and_sleeps_longer():
    """The saving IS the skipped reference poll. A branch that backs off the
    loop but keeps fetching saves nothing, so this pins the source."""
    source = (Path(__file__).resolve().parents[1] / "src" / "btc15_signal"
              / "main.py").read_text(encoding="utf-8")
    block = source[source.index("closed_until = await _venue_closed_until"):]
    block = block[:block.index("if (gap_s >= settings.market_gap_alert_s")]
    assert "venue_closed_poll_seconds" in block
    assert "reference.poll" not in block, \
        "the closed branch must not fetch the reference"
    assert "MARKET_GAP[\"closed_told\"]" in block, "must report once, not on every poll"


def test_reopening_clears_the_closure_state():
    """A closure that has ended must leave nothing behind, or the next genuine
    outage is silently treated as a weekend."""
    source = (Path(__file__).resolve().parents[1] / "src" / "btc15_signal"
              / "main.py").read_text(encoding="utf-8")
    block = source[source.index('told = MARKET_GAP.pop("told", None)'):]
    block = block[:block.index("opened = contract.open_ms")]
    assert 'MARKET_GAP.pop("closed_told", None)' in block
    assert "VENUE_SCHEDULE.clear()" in block

# ------------------------------------ a closure must not END in a false alarm

def test_a_closure_does_not_lapse_before_the_market_reopens():
    """THE DEFECT THIS PINS, found by audit on 2026-09-25 before it could fire.

    `venue_closed_gap_s` decides whether a gap IS a closure. It was also being
    applied to every later poll, so once the reopen came within 30 minutes the
    closure lapsed, and the outage path then reported the whole 48-hour weekend
    as "NO MARKET AT THE EXCHANGE" - a false alarm every Sunday evening, which
    is exactly the alert-fatigue this feature exists to avoid.

    A closure ends when the market opens, not half an hour before it."""
    k = FakeKalshi(next_open=NOW + 48 * HOUR)
    assert closed(k, gap_s=1200) == NOW + 48 * HOUR      # first determination
    # Now walk the clock to 10 minutes before the reopen - inside the 30-minute
    # threshold that used to end the closure early.
    near = NOW + 48 * HOUR - 600_000
    s = sessions()
    still = asyncio.run(_venue_closed_until(k, s, near, gap_s=48 * 3600))
    assert still == NOW + 48 * HOUR, "the closure lapsed before the reopen"
    assert k.calls == 1, "and it must not re-ask Kalshi to know that"


def test_past_the_reopen_with_no_market_becomes_an_outage_again():
    """The other side of the same coin: once the expected reopen has passed and
    there is still no market, that IS a fault and the alert must arm again."""
    k = FakeKalshi(next_open=NOW + 1 * HOUR)
    VENUE_SCHEDULE['next_open_ms'] = NOW + 1 * HOUR
    VENUE_SCHEDULE['asked_ms'] = NOW
    after = NOW + 2 * HOUR          # an hour past the expected reopen
    k.next_open = None              # Kalshi now lists nothing
    assert asyncio.run(_venue_closed_until(k, sessions(), after,
                                           gap_s=2 * 3600)) is None


def test_the_banner_does_not_claim_auto_on_when_it_cannot_execute():
    """The banner was hardcoded, then read the flag - but the flag alone would
    announce "AUTO TRADING ON" on an instance that can place no order. `/auto
    on` refuses that state for the same reason: you would go to sleep believing
    it was trading."""
    source = (Path(__file__).resolve().parents[1] / "src" / "btc15_signal"
              / "main.py").read_text(encoding="utf-8")
    block = source[source.index('if auto_is_on(store, settings):'):]
    block = block[:block.index('print(f"BTC15 signal started')]
    assert 'execution_configured(settings)' in block
    assert 'missing_for_execution(settings)' in block
    assert 'CANNOT EXECUTE' in block
