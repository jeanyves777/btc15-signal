"""Every message says which instrument it is about.

Two instances now write to ONE Telegram chat. A message that does not name
its instrument is ambiguous, and the ambiguous ones are the dangerous ones:
RECOVERY ARMED, the money summaries and the settlement recaps carry no
ticker at all, so the reader has nothing to go on.

THE LABEL IS APPLIED IN `compose` AND NOWHERE ELSE, because compose is the
only assembler. A label that only some builders remembered would be worse
than none - the reader would learn to assume an unlabelled message was the
other instrument.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import surface  # noqa: E402


def plain(text):
    for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>"):
        text = text.replace(tag, "")
    return text


def head(ticker="", header="UP SIGNAL"):
    return plain(surface.compose(
        header=header, ticker=ticker, essentials=[], checks=[], status="",
    )).split("\n")[0]


def setup_function():
    surface.set_instrument("")


# ------------------------------------------------------ derived from ticker

def test_a_btc_ticker_labels_btc():
    assert head("KXBTC15M-26SEP241300-00").startswith("BTC · ")


def test_an_eth_ticker_labels_eth():
    assert head("KXETH15M-26SEP241300-00").startswith("ETH · ")


def test_the_hourly_ladder_is_btc_too():
    """Different series, same instrument. Derived from the ticker body, not
    from a list of series that would need editing for each new product."""
    assert surface.asset("KXBTCD-26SEP2412") == "BTC"


def test_an_unknown_series_is_not_guessed():
    assert surface.asset("KXAAAGASD-26SEP24") == ""
    assert surface.asset("") == ""
    assert surface.asset("NOTAKALSHITICKER") == ""


# ------------------------------- the messages with NO ticker are the point

def test_a_message_without_a_ticker_still_names_its_instrument():
    """RECOVERY ARMED and the money summaries carry no ticker. These are
    exactly the messages that would be ambiguous between two instances."""
    surface.set_instrument("KXETH15M")
    assert head(header="RECOVERY ARMED").startswith("ETH · ")


def test_each_instance_labels_its_own():
    for series, expect in (("KXBTC15M", "BTC"), ("KXETH15M", "ETH")):
        surface.set_instrument(series)
        assert head(header="RECOVERY ARMED").startswith(f"{expect} · ")


def test_the_ticker_wins_over_the_instance_default():
    """If a process ever reported on a market from another series, the
    message must describe the MARKET, not the process."""
    surface.set_instrument("KXBTC15M")
    assert head("KXETH15M-26SEP241300-00").startswith("ETH · ")


# ------------------------------------------- a single instance is unchanged

def test_no_instrument_and_no_ticker_leaves_the_header_alone():
    """A deployment that never calls `set_instrument` must look exactly as
    it did. The label is an addition, not a reformat."""
    assert head(header="UP SIGNAL") == "UP SIGNAL"


# --------------------------------------------- applied once, in one place

def test_the_label_is_applied_only_in_compose():
    import inspect

    source = inspect.getsource(surface)
    # declaration, `global`, assignment in set_instrument - and exactly one
    # READ, which must be the one inside compose.
    assert source.count("_INSTRUMENT") == 4
    assert source.count("or _INSTRUMENT") == 1, "read in more than one place"
    assert "label = asset(ticker) or _INSTRUMENT" in inspect.getsource(
        surface.compose)


def test_the_service_sets_it_at_startup():
    import inspect

    from btc15_signal import main

    source = inspect.getsource(main.service)
    assert "surface.set_instrument(settings.kalshi_series)" in source


def test_every_builder_goes_through_compose():
    """The guarantee the label rests on. If a message were assembled by
    hand it would silently lose its instrument."""
    import inspect

    from btc15_signal import messages

    source = inspect.getsource(messages)
    # the entry, fill and recovery builders all compose
    for fn in ("signal_message", "fill_message", "recovery_armed_message"):
        body = inspect.getsource(getattr(messages, fn))
        assert "surface.compose(" in body, fn
    assert source.count("surface.compose(") >= 3
