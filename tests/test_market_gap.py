"""The silent stop: an exchange with nothing listed, and nobody told.

On 2026-09-24 Kalshi listed no 15-minute market for TWO HOURS. The service was
entirely healthy throughout - the poll loop turned, observations advanced, the
reference recorder kept writing, settlements reconciled - and it correctly
logged "between windows; waiting for the next market". It logged that ONCE, at
the start, and then said nothing for two hours.

The operator's only symptom was that Telegram had gone quiet, and they noticed
about two hours in. Absence of alerts is not an alert, and silence is this
system's worst failure mode precisely because everything looks fine.

A window boundary is SECONDS. Anything past `market_gap_alert_s` is an outage
and gets said out loud, once, with the all-clear when it ends.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import messages  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402


def plain(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", text)


# -------------------------------------------------------- the duration read

def test_a_short_gap_reads_in_minutes():
    assert messages._duration(600) == "10m"
    assert messages._duration(59) == "0m"


def test_a_long_gap_reads_in_hours():
    assert messages._duration(7200) == "2h 00m"
    assert messages._duration(7500) == "2h 05m"


# ------------------------------------------------------------- the message

def test_the_alert_says_how_long():
    text = plain(messages.market_gap_message(7200))
    assert "NO MARKET AT THE EXCHANGE" in text
    assert "2h 00m" in text


def test_the_alert_says_the_service_is_not_broken():
    """Read on a phone at 3am, "no market" looks exactly like a dead bot."""
    text = plain(messages.market_gap_message(1800))
    assert "running normally" in text
    assert "nothing to trade" in text


def test_the_alert_says_an_open_position_is_unaffected():
    text = plain(messages.market_gap_message(1800))
    assert "settles at expiry" in text


def test_the_all_clear_names_the_market_and_the_gap():
    text = plain(messages.market_back_message(7245, "KXBTC15M-26SEP240515-15"))
    assert "MARKETS ARE BACK" in text
    assert "KXBTC15M-26SEP240515-15" in text
    assert "2h 00m" in text


# --------------------------------------------------------- the threshold

def test_the_threshold_is_far_above_a_normal_boundary():
    """Kalshi flips windows in seconds. The default must never fire on one."""
    assert Settings().market_gap_alert_s >= 300


# ------------------------------------------------- the loop is wired to it

def test_the_poll_loop_reports_a_gap_once_and_closes_it():
    import inspect

    from btc15_signal import main

    src = inspect.getsource(main)
    # Opened, reported once, and cleared when a market returns.
    assert 'MARKET_GAP.setdefault("since", now_ms)' in src
    assert "market_gap_alert_s" in src
    assert "market_gap_message" in src
    assert "market_back_message" in src
    # The alert is keyed on the gap's start, so one outage is one message.
    assert 'MARKET_GAP.get("told")' in src


def test_the_gap_is_measured_from_the_first_poll_that_found_nothing():
    """`setdefault` and not assignment - otherwise the clock restarts every
    poll and the gap can never grow past one interval."""
    import inspect

    from btc15_signal import main

    src = inspect.getsource(main)
    at = src.index('MARKET_GAP.setdefault("since", now_ms)')
    window = src[at - 400:at]
    assert 'MARKET_GAP["since"] = now_ms' not in window
