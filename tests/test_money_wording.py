"""The money on two messages about one trade must be the same money.

Each of these pins a defect that rendered correctly and read wrongly - the
arithmetic was never in question, the wording was, and wording is the entire
product here because it is what the operator acts on at three in the morning.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import messages, surface  # noqa: E402

TICKER = "KXBTC-26SEP23-B112500"


def plain(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", text)


# ------------------------------------------------- the fee is a money figure

def test_a_two_cent_fee_prints_as_two_cents():
    """`$0.0200` claims a precision the account statement does not have."""
    line = surface.entry_cost(2, 0.80, 0.02)
    assert "$0.02 fees" in line
    assert "0.0200" not in line


def test_a_sub_cent_fee_is_named_rather_than_rounded_to_zero():
    """Rounding it to `$0.00` says there was no fee, and there was."""
    line = surface.entry_cost(1, 0.80, 0.003)
    assert "$0.00 fees" not in line
    assert "under 1¢" in line


def test_the_cost_includes_the_fee_and_says_so():
    line = plain(surface.entry_cost(2, 0.80, 0.02))
    assert "$1.62" in line, "stake plus the charged fee"
    assert "incl." in line


def test_without_a_fee_the_cost_says_it_is_before_fees():
    """An unlabelled gross figure is the defect, not the missing fee."""
    line = plain(surface.entry_cost(2, 0.80, None))
    assert "$1.60" in line
    assert "before fees" in line


# --------------------------------------- one trade, one cost, two messages

def _recap(**over):
    base = dict(
        side="UP", ticker=TICKER, winner="UP", won=True, traded=True,
        pnl=0.13, contracts=2, paid=0.80, fee=0.02, called_side="UP",
        qualified=True,
    )
    base.update(over)
    return plain(messages.result_message(**base))


def test_the_recap_reports_the_cost_the_fill_announced():
    """The fill said $1.62 and the recap said $1.60 for the same two
    contracts. Neither message named which of them the fee was inside, so the
    2c gap read as an unexplained discrepancy in the ledger."""
    fill = plain(surface.entry_cost(2, 0.80, 0.02))
    assert "$1.62" in fill
    assert "$1.62" in _recap()


def test_a_recap_without_a_fee_falls_back_to_the_stake():
    """Missing is missing. Inventing a fee to make the two agree would make
    them agree on a number neither of them measured."""
    assert "$1.60" in _recap(fee=None)


# ----------------------------------------------- zero net is not a verdict

def test_a_zero_net_early_exit_closes_rather_than_selling_early():
    """`SOLD EARLY - +$0.00` signs zero and offers a verdict where there is
    none. The market still settled UP or DOWN; the TRADE netted nothing."""
    text = _recap(pnl=0.0, exited_at=0.81, winner="DOWN", won=False)
    assert "CLOSED · $0.00 net" in text
    assert "SOLD EARLY" not in text
    assert "+$0.00" not in text


def test_the_early_exit_is_still_stated_in_the_body():
    """Changing the headline must not lose the fact that it was sold."""
    text = _recap(pnl=0.0, exited_at=0.81, winner="DOWN", won=False)
    assert "Sold before expiry" in text


def test_a_profitable_early_exit_still_headlines_the_sale():
    text = _recap(pnl=0.21, exited_at=0.92, winner="DOWN", won=False)
    assert "SOLD EARLY · +$0.21" in text


def test_break_even_is_never_offered_as_a_market_outcome():
    """The market settles UP or DOWN, always. `Break-even` describes a trade's
    net, and the operator ruled it out as an outcome word entirely."""
    for over in ({"pnl": 0.0}, {"pnl": 0.0, "exited_at": 0.80},
                 {"pnl": 0.13}, {"pnl": -0.13, "won": False}):
        assert "break-even" not in _recap(**over).lower()


def test_an_untraded_signal_says_not_traded():
    text = _recap(traded=False, pnl=None, contracts=0.0, paid=None,
                  winner="DOWN", won=False, qualified=False)
    assert "Not traded" in text
    assert "CLOSED" not in text
