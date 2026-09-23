"""The corpus must evaluate the DEPLOYED policy, not a single minute of it.

The bot does not judge a market once. It scans every poll from 660s to 360s
remaining, takes the first minute whose gates pass, alerts once and stops. A
market refused at 11 minutes and qualified at 8 is a market it TRADES.

Two wrong ways to summarise that, and this pins the line between them:

    one row at 660s        pessimistic - counts every later entry a refusal
    one row per poll       optimistic  - six correlated copies per market,
                                         every sample count inflated sixfold

Both were live in this repo at different times. The rule is one row per
market, chosen by walking the window in the order the bot sees it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from brti_dataset import choose_minute  # noqa: E402


def minute(remaining_s: int, qualifies: bool, ask: float = 0.80) -> dict:
    return {"remaining_s": remaining_s, "rule_match": int(qualifies),
            "our_ask": ask}


WINDOW = (660, 600, 540, 480, 420, 360)


def test_a_market_refused_early_and_qualified_later_is_traded():
    """The whole point. First-minute analysis calls this a refusal; the bot
    buys it at 540s."""
    minutes = [minute(660, False), minute(600, False),
               minute(540, True), minute(480, True)]
    chosen = choose_minute(minutes, policy=True)
    assert chosen["rule_match"] == 1
    assert chosen["remaining_s"] == 540, "the FIRST qualifying minute, not a later one"


def test_first_minute_analysis_calls_the_same_market_a_refusal():
    """Kept deliberately, because it answers a different question - and any
    number computed from it has to be labelled first-minute."""
    minutes = [minute(660, False), minute(540, True)]
    assert choose_minute(minutes, policy=False)["rule_match"] == 0


def test_each_market_contributes_exactly_one_row():
    for policy in (True, False):
        chosen = choose_minute([minute(s, True) for s in WINDOW], policy=policy)
        assert isinstance(chosen, dict), "one row, not a list of polls"


def test_the_bot_stops_at_the_first_qualifying_minute():
    """It alerts once per window - 'Already alerted this window' - so a later
    and better price is not available to it."""
    minutes = [minute(660, True, ask=0.90), minute(600, True, ask=0.72)]
    assert choose_minute(minutes, policy=True)["our_ask"] == 0.90


def test_a_market_that_never_qualifies_is_priced_at_the_first_look():
    """Not arbitrary: with the gates removed the first poll qualifies by
    construction, so 660s is where the policy would have entered without
    them - which is what the refused leg measures."""
    minutes = [minute(s, False) for s in WINDOW]
    chosen = choose_minute(minutes, policy=True)
    assert chosen["rule_match"] == 0
    assert chosen["remaining_s"] == 660


def test_a_market_qualifying_only_at_the_last_minute_is_still_traded():
    minutes = [minute(s, False) for s in WINDOW[:-1]] + [minute(360, True)]
    chosen = choose_minute(minutes, policy=True)
    assert chosen["rule_match"] == 1 and chosen["remaining_s"] == 360


def test_a_market_with_no_usable_minute_is_dropped():
    assert choose_minute([], policy=True) is None
    assert choose_minute([], policy=False) is None


def test_the_policy_never_qualifies_fewer_markets_than_first_minute():
    """Scanning more minutes can only find more entries, never fewer. If this
    inverted, the walk would be reading the window backwards."""
    minutes = [minute(660, False), minute(600, True)]
    assert choose_minute(minutes, policy=True)["rule_match"] >= \
        choose_minute(minutes, policy=False)["rule_match"]
