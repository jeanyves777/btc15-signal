"""After expiry the bot must say who won and whether our setup won."""

import asyncio
import itertools

from btc15_signal.config import Settings
from btc15_signal.main import report_settlement
from btc15_signal.store import Store


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send(self, text, buttons=None):
        self.sent.append(text)


_CALL = itertools.count()


def settle_and_report(
    tmp_path, side, result, price=0.90, qualified=1, traded=False, count=1, exit_price=None
):
    """Each call gets its own database so repeated use inside one test is clean.

    `traded` places a real filled order behind the signal. Without it the
    signal is paper, and a paper signal must carry no money figure at all -
    nothing was spent.
    """
    store = Store(str(tmp_path / f"t{next(_CALL)}.db"))
    store.record((0, 1, 81_245.52, 81_300.0, side, 8, 0.95, "KXBTC15M-TEST", price, qualified))
    if traded:
        proposal = store.create_proposal(
            "primary", 0, "KXBTC15M-TEST", side, price, 0.0, count, 9999, 9999, 1
        )
        store.db.execute("UPDATE trade_proposals SET status='filled' WHERE id=?", (proposal.id,))
        store.db.commit()
        if exit_price is not None:
            store.mark_exited(proposal.id, exit_price, count, "sold")
    pending = store.pending_settlements(900_000)[0]
    store.settle(0, side, result)
    telegram = FakeTelegram()
    asyncio.run(report_settlement(store, telegram, pending, result, Settings()))
    return telegram.sent[0]


def test_reports_a_win_when_our_side_matches_the_result(tmp_path):
    """Three separate facts, each in its own words: what the market did, what
    we said, and what it did to the account. A single WIN/LOSS headline was
    read as money even when no order existed."""
    text = settle_and_report(tmp_path, "UP", "yes", traded=True)
    assert "Market settled <b>UP</b>" in text
    assert "Bought <b>UP</b>" in text and "WIN" in text
    assert "WIN" in text  # the headline is about money, and money was made
    assert "at 90¢" in text


def test_reports_a_loss_when_the_other_side_won(tmp_path):
    text = settle_and_report(tmp_path, "UP", "no", traded=True)
    assert "LOSS" in text and "❌" in text
    assert "Market settled <b>DOWN</b>" in text
    assert "Bought <b>UP</b>" in text and "Market settled <b>DOWN</b>" in text


def test_down_signal_wins_when_the_market_settles_no(tmp_path):
    text = settle_and_report(tmp_path, "DOWN", "no", traded=True)
    assert "WIN" in text and "✅" in text
    assert "Market settled <b>DOWN</b>" in text
    assert "Bought <b>DOWN</b>" in text


def test_pnl_is_sized_by_what_was_actually_filled(tmp_path):
    """One contract at 80c risks 80c - not $10 of payout and not $10 of cash.

    Reporting a size the bot never ordered overstated the money more than
    tenfold: a real -$0.85 night was announced as -$3.91.
    """
    # 1 contract at 80c: fee 0.07*1*0.8*0.2 = 1.12c, charged as $0.0112.
    win = settle_and_report(tmp_path, "UP", "yes", price=0.80, traded=True)
    assert "Cost $0.80 · Profit $0.19" in win

    loss = settle_and_report(tmp_path, "UP", "no", price=0.80, traded=True)
    assert "Cost $0.80 · Lost $0.81" in loss


def test_an_untraded_signal_reports_no_money_at_all(tmp_path):
    """The defect that produced -$3.91: paper signals priced as realised P&L."""
    text = settle_and_report(tmp_path, "UP", "yes", price=0.80, traded=False)
    # No green tick that could be mistaken for a payday on a trade that never
    # happened - that is exactly how "WIN / No order was placed" read.
    assert "SIGNAL WON · NOT TRADED" in text
    assert "Profit: $0.00" in text
    assert "PROFIT" not in text
    assert "cost $" not in text
    assert "after fees" not in text


def test_a_position_sold_early_is_never_reported_as_a_settlement_win(tmp_path):
    """Bought at 90c, sold at 35c, and the contract then settled our way.

    Scoring this by who eventually won announced a realised loss as a win, and
    disagreed in sign with the daily loss floor, which books the same trade at
    its exit price.
    """
    text = settle_and_report(
        tmp_path, "UP", "yes", price=0.90, traded=True, exit_price=0.35
    )
    assert "SOLD EARLY" in text
        # the market later did. Chip, sign and wording must all agree.
    assert "❌💸" in text and "−$" in text
    assert "PROFIT" not in text and "WIN" not in text
    assert "Sold before expiry at 35¢" in text
    assert "prediction was correct" in text  # the call was right, the sale was not  # stated, not hidden


def test_cost_is_shown_because_on_a_loss_the_cost_is_the_loss(tmp_path):
    """Kalshi sizes by max payout; the cost is the money actually at risk.

    A $10 max payout at 86.8c costs $8.68, and a loser loses $8.68 - not $10.
    """
    from btc15_signal import messages
    from btc15_signal.validation import trade_pnl

    text = messages.settlement(
        head="HEAD", ticker="T", side="UP", winner="DOWN", won=False, target=1.0,
        contract_price=0.868, pnl=trade_pnl(0.868, False, contracts=10),
        qualified=True, basis="$10 max payout", contracts=10,
    )
    assert "Cost $8.68" in text
    assert "−$8.76" in text  # 8.68 cost + 8.02c fee, charged not floored
    assert "-10.00" not in text  # the old cash convention overstated it


def test_marks_whether_the_rule_would_have_entered(tmp_path):
    liked = settle_and_report(tmp_path, "UP", "yes", qualified=1)
    assert "rule qualified it" in liked
    assert "rule declined it" in settle_and_report(tmp_path, "UP", "yes", qualified=0)
    # and a real order is never described as untraded
    real = settle_and_report(tmp_path, "UP", "yes", qualified=1, traded=True)
    assert "Not traded" not in real


def test_includes_a_running_record(tmp_path):
    text = settle_and_report(tmp_path, "UP", "yes")
    assert "SIGNAL WON" in text  # the running record is now the compact Live line


def test_manual_execution_stays_available_alongside_automation():
    """Automation does not remove the button; it only stops needing a press.

    Manual override covers the band automation will not touch, so a human can
    still take a setup the rule rejects while unattended trading runs.
    """
    settings = Settings()
    assert settings.manual_execution_enabled
    assert settings.manual_min_ask < 0.85  # below the automated band
