"""Every message the trading loop sends comes off the one surface.

These pin the migration itself, not the formatter: a builder can be perfect
and still never be called, which is exactly what "it still looks the same as
before" meant - the v2 surface existed and three send sites went on writing
their own strings inline.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal import messages, surface  # noqa: E402
from btc15_signal.notify import Notifier  # noqa: E402

MAIN = (ROOT / "src" / "btc15_signal" / "main.py").read_text(encoding="utf-8")


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# ------------------------------------------------ no inline strings survive

def test_no_send_site_writes_a_headline_inline():
    """A headline typed at the call site cannot carry the money footer, the
    rotation or the duplicate suppression, because none of those are there."""
    for orphan in ("AUTOMATION IS OFF AT THE STRATEGY",
                   "AUTO ORDER NOT FILLED"):
        assert orphan not in MAIN, f"{orphan} is still written inline"


def test_the_trading_loop_no_longer_calls_the_v1_fill_builder():
    assert "messages.order_filled(" not in MAIN


def test_the_fill_goes_out_through_the_notifier():
    """Claim-before-send, so a retry cannot announce one position twice."""
    assert 'notifier.send_once(\n                                "fill"' in MAIN


# ------------------------------------------------------- what they render

def test_the_unfilled_order_claims_no_position():
    text = plain(messages.not_filled_message(
        ticker="KXBTC-T", note="ask moved to 95c", snapshot=None))
    assert "nothing spent" in text
    assert "No contracts bought" in text
    # Nothing that reads as a cost, a size or a holding.
    assert "Cost" not in text and "contracts filled" not in text


def test_the_unfilled_order_repeats_the_broker_note():
    text = plain(messages.not_filled_message(
        ticker="KXBTC-T", note="ask moved to 95c", snapshot=None))
    assert "95¢" in text, "and in the surface's own typography"


def test_the_automation_off_alert_names_both_switches():
    """One switch on and one off is the whole point: the alert exists because
    `/auto` being ON is what made four silent hours look fine."""
    text = plain(messages.automation_off_message(ask=0.80, snapshot=None))
    assert "/auto is ON" in text
    assert "enabled: false" in text
    assert "80¢" in text


def test_both_new_messages_carry_the_money_footer():
    """The footer is permanent. A message without it is a message the reader
    has to leave to find out where they stand."""
    for text in (messages.not_filled_message(ticker="T", note="n",
                                             snapshot=None),
                 messages.automation_off_message(ask=0.8, snapshot=None)):
        assert surface.DIVIDER in text
        assert surface.MONEY in text


# --------------------------------------------------- the size and its reason

def test_the_fill_names_why_the_size_was_what_it_was():
    text = plain(messages.fill_message(
        side="UP", ticker="T", contracts=2, paid=0.80, fee=0.02,
        remaining=300, confidence="HIGH", facts=[], snapshot=None,
        size_reason="recovery upsize: +$2.00"))
    assert "2 contracts filled" in text
    assert "recovery upsize" in text


def test_a_fill_without_a_reason_adds_no_parenthetical():
    text = plain(messages.fill_message(
        side="UP", ticker="T", contracts=1, paid=0.80, fee=0.01,
        remaining=300, confidence="HIGH", facts=[], snapshot=None))
    assert "1 contract filled at 80¢" in text
    assert "(" not in text.split("\n")[2]


# ------------------------------------------- an average is not a fill price

def test_an_unconfirmed_price_reports_no_slippage():
    """`paid` without per-fill confirmation is the ORDER AVERAGE. Comparing an
    average against the decision ask prints a slippage figure that was never
    priced, on a line that looks like a measurement."""
    text = plain(messages.fill_message(
        side="UP", ticker="T", contracts=2, paid=0.80, fee=0.02,
        remaining=300, confidence="HIGH", facts=[], snapshot=None,
        decision_ask=None))
    assert "Decision ask" not in text


def test_a_confirmed_price_does_report_slippage():
    text = plain(messages.fill_message(
        side="UP", ticker="T", contracts=2, paid=0.80, fee=0.02,
        remaining=300, confidence="HIGH", facts=[], snapshot=None,
        decision_ask=0.81))
    assert "Decision ask 81¢" in text
    assert "better" in text


# ------------------------------------------------- the ambiguity resolution

def test_a_fill_is_resent_when_a_claim_is_left_unresolved():
    """A position the operator does not know about is worse than one they are
    told about twice. The duplicate can be read past; the silence cannot."""
    assert "fill" in Notifier.RESEND_ON_AMBIGUITY
    assert "not_filled" in Notifier.RESEND_ON_AMBIGUITY


def test_a_signal_is_not_resent():
    """The next poll supersedes it, so a stale duplicate is worse than a gap."""
    assert "signal" not in Notifier.RESEND_ON_AMBIGUITY
    assert "automation_off" not in Notifier.RESEND_ON_AMBIGUITY


# --------------------------------------------------------- keys are unique

def test_the_fill_key_names_the_proposal_not_just_the_window():
    """One window can hold more than one order once an add fires. Keying on
    the window alone would suppress the second as a duplicate of the first."""
    tree = ast.parse(MAIN)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "send_once"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value not in ("fill", "not_filled"):
            continue
        found.append(ast.unparse(node.args[1]))
    assert found, "no fill send site found"
    for key in found:
        assert "claimed.id" in key, f"{key} cannot tell two orders apart"


# ---------------------------------------------------- the exit family

def _cash(**over):
    base = dict(ticker="T", side="UP", paid=0.80, bid=0.91, count=2,
                captured=0.20, remaining=120, note="", sold=True,
                entry_fee=0.02, exit_fee=0.02, snapshot=None)
    base.update(over)
    return plain(messages.cash_out_message(**base))


def test_a_cash_out_states_what_it_banked_and_what_it_gave_up():
    """Selling at 91c after paying 80c banks money AND forgoes the rest.
    Reading only the first half makes the rule look better than it is."""
    text = _cash()
    assert "Banked +$0.18" in text
    assert "Gave up the last $0.18" in text
    assert "net of fees" in text


def test_a_failed_cash_out_reads_as_a_miss():
    """One printed `Banked +0.20` directly under `STILL HOLDING`, describing
    a sale that did not happen and contradicting its own headline."""
    text = _cash(sold=False)
    assert "Nothing sold" in text
    assert "WOULD have banked" in text
    assert "Still fully exposed" in text
    assert "Banked +" not in text


def test_cash_out_prices_are_cents_not_percentages():
    """80% is a probability. The trade paid 80 cents."""
    text = _cash()
    assert "bought 80¢, sold 91¢" in text
    assert "80%" not in text


def test_banked_money_carries_a_currency_symbol():
    """It printed `Banked +0.18` - a number with no unit, on a money line."""
    assert "Banked +0.18 " not in _cash()


def test_the_exit_event_does_not_wear_a_direction_chip():
    """`auto_exit` headlined with the DOWN chip while its body said `Held
    UP`, so one message carried two contradictory directions."""
    text = messages.auto_exit_message(
        ticker="T", side="UP", price=112470.0, target=112500.0, bid=0.55,
        remaining=90, note="", sold=True, snapshot=None)
    assert text.startswith(surface.EXIT)
    assert not text.startswith(surface.DOWN)
    assert surface.UP in text, "the position's direction is still stated"


def test_a_warning_that_placed_no_order_says_so():
    """A warning that looks like an execution is how an operator comes to
    believe a position was closed when it is still open."""
    text = plain(messages.exit_warning_message(
        ticker="T", side="UP", price=112470.0, target=112500.0, bid=0.55,
        remaining=90, snapshot=None))
    assert "No order was placed" in text
    assert "STILL HOLDING" in text
    assert "sold" not in text.lower()


def test_the_exit_family_carries_the_money_footer():
    for text in (messages.cash_out_message(
                     ticker="T", side="UP", paid=0.8, bid=0.9, count=1,
                     captured=0.2, remaining=60, note="", sold=True,
                     snapshot=None),
                 messages.auto_exit_message(
                     ticker="T", side="UP", price=1.0, target=1.0, bid=0.5,
                     remaining=60, note="", sold=True, snapshot=None),
                 messages.exit_warning_message(
                     ticker="T", side="UP", price=1.0, target=1.0, bid=0.5,
                     remaining=60, snapshot=None)):
        assert surface.DIVIDER in text
        assert surface.MONEY in text
        assert "New York" not in text, "the footer says Today, once"


def test_the_trading_loop_no_longer_calls_the_v1_exit_builders():
    for old in ("messages.cash_out(", "messages.auto_exit(",
                "messages.exit_alert("):
        assert old not in MAIN, f"{old} is still wired in"


def test_closing_a_position_is_resent_when_a_claim_is_unresolved():
    """Believing you still hold something you sold costs as much as the
    reverse."""
    assert "cash_out" in Notifier.RESEND_ON_AMBIGUITY
    assert "auto_exit" in Notifier.RESEND_ON_AMBIGUITY
    # The warning placed no order, and the next poll re-raises it.
    assert "exit_warning" not in Notifier.RESEND_ON_AMBIGUITY
