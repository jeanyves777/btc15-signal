"""The recap must report the side we HELD, not the side we CALLED.

2026-09-23, KXBTC15M-26SEP231215-15. Two contracts of UP filled at 93.2c, the
market settled YES, Kalshi paid out $2.00 and booked a profit of $0.1271. The
Telegram recap said:

    ❌💸 LOSS · −$1.87
    🔴⬇️ Bought DOWN at 93¢

Both halves wrong, and wrong by $2.00 in sign. The ledger was correct the whole
time because it syncs from the broker, so the account was never misstated - only
the sentence was, which is the harder kind to notice.

The cause: `predictions` is keyed on `window_open` with INSERT OR IGNORE and is
written at ALERT time, so it holds the FIRST side the reference named. The
reference flipped to UP before the order filled; the row kept DOWN; the recap
computed `won = called_side == winner` and inverted the verdict.

It had happened once before, on 2026-09-21, and gone unnoticed: 2 of 131 traded
markets, both reported as losses, both actually wins.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from btc15_signal import messages, surface  # noqa: E402

TICKER = "KXBTC15M-26SEP231215-15"


def test_the_recap_reports_the_side_that_owned_the_money():
    """The real market, with the broker's own numbers."""
    text = messages.result_message(
        side="UP",                 # what `trade_for_window` says we HELD
        ticker=TICKER,
        winner="UP",
        won=True,
        traded=True,
        pnl=0.1271,                # the broker's pnl, not a reconstruction
        paid=0.932,
        snapshot=None,
    )
    assert text.startswith(surface.WON_MONEY), "a paid-out market is not a loss"
    assert "Took <b>UP</b>" in text
    assert "DOWN" not in text.split(surface.DIVIDER)[0]
    assert "Realised <b>+$0.13</b>" in text
    assert "−$1.87" not in text and "-1.87" not in text


def test_a_flipped_window_is_reported_as_two_verdicts_not_one():
    """When the call and the position disagree, both are stated.

    Hiding the disagreement is what let the inverted recap look ordinary.
    """
    line = surface.priority_lines(
        partial="Signal called DOWN; the position held UP")
    assert line and "called DOWN" in line[0] and "held UP" in line[0]


def test_report_settlement_prefers_the_trades_side_over_the_predictions(
        monkeypatch, tmp_path):
    """Drive the real function and confirm which side reaches the message."""
    import asyncio

    from btc15_signal import main
    from btc15_signal.config import Settings
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "t.db"))
    now = 1_790_179_200_000

    # The archive as it really was: the CALL was DOWN...
    pending = (now, "DOWN", TICKER, 0.932, 0, 84_005.77)
    # ...and the POSITION was UP.
    monkeypatch.setattr(store, "trade_for_window", lambda w: {
        "status": "filled", "count": 2.0, "paid": 0.932, "fee": 0.0089,
        "exit_price": None, "exit_count": None, "side": "UP",
    })
    monkeypatch.setattr(store, "stored_deficit", lambda: None)

    captured = {}

    class FakeTelegram:
        async def send(self, text, buttons=None):
            captured["text"] = text
            return 1

    asyncio.run(main.report_settlement(
        store, FakeTelegram(), pending, "yes", Settings(), now_ms=now,
    ))
    text = captured["text"]
    assert "Took <b>UP</b>" in text, "the recap must name the side we held"
    assert text.startswith(surface.WON_MONEY), "settled YES holding UP is a win"
    # And the call is still reported, because it was genuinely wrong.
    assert "called DOWN" in text and "held UP" in text


def test_an_untraded_signal_is_still_scored_on_the_call():
    """With no position there is no money, so the CALL is the only verdict."""
    text = messages.result_message(
        side="DOWN", ticker=TICKER, winner="UP", won=False, traded=False,
        pnl=None, snapshot=None,
    )
    assert text.startswith(surface.LOST_PAPER)
    assert "No trade · realised P&amp;L $0.00" in text
    assert "Cost" not in text


def test_the_money_figure_is_the_brokers_and_is_never_rebuilt():
    """+$0.1271 is what Kalshi paid. Nothing here recomputes it from prices."""
    text = messages.result_message(
        side="UP", ticker=TICKER, winner="UP", won=True, traded=True,
        pnl=0.1271, paid=0.932, snapshot=None,
    )
    assert "+$0.13" in text
    # 2 x (1.00 - 0.932) = 0.136 before fees; the broker's 0.1271 is net and is
    # the number shown.
    assert "+$0.14" not in text
