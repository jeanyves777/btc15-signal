"""Telegram rejects a message whose HTML it cannot parse - with a 400.

The service catches HTTPError per cycle and logs it, so a malformed tag would
not crash anything; the alert would simply never arrive. That failure is silent,
so it gets a test.
"""

from html.parser import HTMLParser

import pytest

from btc15_signal import messages

# Telegram's entire allowed set for parse_mode=HTML.
ALLOWED = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "span", "tg-spoiler", "a", "tg-emoji", "code", "pre", "blockquote",
}


class Checker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.bad = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.bad.append(f"disallowed <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.bad.append(f"unbalanced </{tag}>")
        else:
            self.stack.pop()


def assert_telegram_html(text: str) -> None:
    checker = Checker()
    checker.feed(text)
    checker.close()
    assert not checker.bad, f"{checker.bad} in:\n{text}"
    assert not checker.stack, f"unclosed {checker.stack} in:\n{text}"
    # A bare '<' that is not a tag also trips Telegram's parser.
    assert "< " not in text


HEAD = messages.scoreboard(15, 12, 0.28, "1 contract")

CASES = [
    HEAD,
    messages.scoreboard(0, 0, 0.0),
    messages.scoreboard(3, 0, -3.0),
    messages.entry_alert(
        head=HEAD, live=True, side="UP", ask=0.91, price=81_494.65, target=81_158.49,
        remaining=432, ticker="KXBTC15M-26SEP202015-15", model=0.963, observed=0.8, samples=5,
    ),
    messages.entry_alert(
        head=HEAD, live=False, side="DOWN", ask=0.88, price=80_900.0, target=81_000.0,
        remaining=61, ticker="KXBTC15M-X", model=0.5, observed=0.0, samples=0,
        note="Not auto-validated - your press is the decision.",
    ),
    messages.no_entry_alert(
        head=HEAD, side="UP", ask=0.5, price=1.0, target=1.0, remaining=330,
        reason="contract price band", model=0.5, observed=0.5, samples=2,
    ),
    messages.reversion_alert(
        head=HEAD, live=False, side="UP", entry=0.33, take_profit=0.5, key_level=81_517.45,
        spike_bps=16.1, rejection_bps=8.5, remaining=727, note="holdout not passed",
    ),
    messages.exit_alert(
        head=HEAD, ticker="KXBTC15M-Y", side="UP", price=81_100.0, target=81_158.49,
        remaining=240, bid=0.42,
    ),
    messages.settlement(
        head=HEAD, ticker="KXBTC15M-Z", side="UP", winner="UP", won=True, target=81_158.49,
        contract_price=0.9, pnl=1.01, qualified=True, basis="1 contract",
    ),
    messages.settlement(
        head=HEAD, ticker="KXBTC15M-Z", side="DOWN", winner="UP", won=False, target=1.0,
        contract_price=None, pnl=None, qualified=False, basis="1 contract",
    ),
    messages.order_result(
        status="filled", side="UP", count=2, note="Filled 2 @ 0.91", order_id="ord-1",
    ),
    messages.order_result(status="rejected", side="UP", count=0, note="x", order_id=None),
    messages.status(head=HEAD, execution_ready=True, manual=True, window=(630, 330)),
]


@pytest.mark.parametrize("text", CASES, ids=range(len(CASES)))
def test_every_message_is_valid_telegram_html(text):
    assert_telegram_html(text)


def test_hostile_text_cannot_inject_markup():
    """A rejection reason is free text; it must never become live markup."""
    nasty = '<script>x</script> & "quoted" <b>bold'
    text = messages.no_entry_alert(
        head=HEAD, side="UP", ask=0.5, price=1.0, target=1.0, remaining=60,
        reason=nasty, model=0.5, observed=0.5, samples=1,
    )
    assert_telegram_html(text)
    assert "<script>" not in text


def test_an_unclosed_tag_would_be_caught():
    """Guard the guard: the checker must actually fail on bad markup."""
    with pytest.raises(AssertionError):
        assert_telegram_html("<b>never closed")
    with pytest.raises(AssertionError):
        assert_telegram_html("<div>disallowed</div>")
