"""The settlement recap: money from the broker, side from the position.

Three facts, never collapsed into one verdict:

    WIN / LOSS / CLOSED       the broker's realised P&L after fees. "Closed"
                              means the TRADE netted exactly zero - the market
                              itself always settles UP or DOWN.
    Bought UP / DOWN          the side actually HELD
    prediction correct/wrong  the recorded signal's side vs the settlement

Using the held side alone is not enough either: an early exit banks money on a
position whose side can still lose at settlement, so the money word comes from
the money and nothing else.

THE DEFECT THIS PINS. `predictions` is keyed on `window_open` with INSERT OR
IGNORE and written at ALERT time, so it holds the FIRST side the reference
named. When the reference flipped before the order filled, `report_settlement`
computed `won = called_side == winner` and handed that inverted flag to
`position_pnl` - turning a paid-out win into a reported loss. It reached the
operator twice on real money. `daily_ledger` was correct throughout because it
syncs from the broker, so the account never drifted; only the sentence did.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import messages  # noqa: E402

WON_MONEY = "✅\U0001f4b0"
LOST_MONEY = "❌\U0001f4b8"
FLAT = "⚪"


def recap(**kw):
    args = dict(head="", ticker="KXBTC15M-26SEP231215-15", side="UP",
                called_side="UP", winner="UP", won=True, target=84_005.77,
                contract_price=0.932, pnl=0.1271, qualified=0, contracts=2.0)
    args.update(kw)
    return messages.settlement(**args)


# ------------------------------------------------ the two affected markets


def test_the_2026_09_23_market_reads_as_the_win_kalshi_paid():
    """2 contracts of UP at 93.2c, settled YES, Kalshi paid $2.00, +$0.1271.

    Reported at the time as: `❌💸 LOSS · −$1.87` / `🔴⬇️ Bought DOWN at 93¢`.
    """
    text = recap(side="UP", called_side="DOWN", winner="UP", pnl=0.1271,
                 contract_price=0.932, contracts=2.0)
    assert text.startswith(WON_MONEY)
    assert "WIN · +$0.13" in text
    assert "Bought <b>UP</b>" in text
    assert "Bought <b>DOWN</b>" not in text
    assert "−$1.87" not in text
    # The call WAS wrong, and says so on its own line.
    assert "DOWN prediction was wrong" in text
    assert "Signal called DOWN; the position held UP" in text


def test_the_2026_09_21_market_reads_as_the_win_kalshi_paid():
    """The earlier one, inverted the other way: called UP, held DOWN, +$0.2821."""
    text = recap(ticker="KXBTC15M-26SEP212300-00", side="DOWN",
                 called_side="UP", winner="DOWN", pnl=0.2821,
                 contract_price=0.80, contracts=2.0)
    assert text.startswith(WON_MONEY)
    assert "WIN · +$0.28" in text
    assert "Bought <b>DOWN</b>" in text
    assert "UP prediction was wrong" in text
    assert "Signal called UP; the position held DOWN" in text


# --------------------------------------- a side change before execution


def test_a_side_change_before_execution_reports_both_sides():
    text = recap(side="UP", called_side="DOWN", winner="UP", pnl=0.13)
    assert "Bought <b>UP</b>" in text          # the position
    assert "DOWN prediction was wrong" in text  # the call
    assert "the position held UP" in text


def test_no_flip_means_no_confusing_extra_line():
    text = recap(side="UP", called_side="UP", winner="UP", pnl=0.13)
    assert "UP prediction was correct" in text
    assert "the position held" not in text


# ------------------------- a profitable exit that later settles against us


def test_a_profitable_early_exit_stays_a_win_when_the_side_later_loses():
    """The case the held side alone cannot answer.

    Bought DOWN at 86c, sold at 99.7c, market later settled UP. The position's
    side LOST and the trade MADE money. The money word must follow the money.
    """
    text = recap(side="DOWN", called_side="DOWN", winner="UP", won=False,
                 pnl=0.2566, contract_price=0.86, exited_at=0.997)
    assert text.startswith(WON_MONEY)
    assert "SOLD EARLY · +$0.26" in text
    assert "Bought <b>DOWN</b>" in text
    assert "DOWN prediction was wrong" in text
    assert "remained profitable because it exited early" in text
    assert "already counted at the sale" in text
    assert "LOSS" not in text


def test_a_losing_early_exit_stays_a_loss_when_the_side_later_wins():
    text = recap(side="UP", called_side="UP", winner="UP", won=True,
                 pnl=-0.31, contract_price=0.84, exited_at=0.55)
    assert text.startswith(LOST_MONEY)
    assert "SOLD EARLY · −$0.31" in text
    assert "UP prediction was correct" in text
    assert "still lost because it exited below cost" in text


# ------------------------------------------------------- break-even


def test_a_zero_net_trade_is_closed_not_a_third_market_outcome():
    """The market settles UP or DOWN, always. "Closed" is about the TRADE's
    net after fees being exactly zero - it is not something the market did."""
    text = recap(pnl=0.0, contract_price=0.50, contracts=2.0, winner="UP")
    assert text.startswith(FLAT)
    assert "CLOSED · $0.00 net" in text
    assert "BREAK-EVEN" not in text
    assert "WIN" not in text and "LOSS" not in text
    # The market outcome still reads UP, on its own line.
    assert "Market settled <b>UP</b>" in text


# --------------------------------------- the money comes from the broker


def test_report_settlement_takes_the_held_side_and_the_booked_money(
        monkeypatch, tmp_path):
    import asyncio

    from btc15_signal import main
    from btc15_signal.config import Settings
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "t.db"))
    now = 1_790_179_200_000
    ticker = "KXBTC15M-26SEP231215-15"

    monkeypatch.setattr(store, "trade_for_window", lambda w: {
        "status": "filled", "count": 2.0, "paid": 0.932, "fee": 0.0089,
        "exit_price": None, "exit_count": None, "side": "UP",
        "confirmed": True,
    })
    # The broker booked +0.1271. A local rebuild from the inverted flag would
    # have produced -1.87; the ledger figure must win.
    monkeypatch.setattr(store, "realised_for_ticker",
                        lambda t: 0.1271 if t == ticker else None)

    captured = {}

    class FakeTelegram:
        async def send(self, text, buttons=None):
            captured["text"] = text
            return 1

    asyncio.run(main.report_settlement(
        store, FakeTelegram(),
        (now, "DOWN", ticker, 0.932, 0, 84_005.77),   # the CALL was DOWN
        "yes", Settings(),
    ))
    text = captured["text"]
    assert "WIN" in text and "+$0.13" in text
    assert "Bought <b>UP</b>" in text
    assert "DOWN prediction was wrong" in text
    assert "LOSS" not in text


def test_an_unbooked_market_keeps_its_provisional_figure(monkeypatch, tmp_path):
    """None from the ledger means "not booked yet", not zero."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "t.db"))
    assert store.realised_for_ticker("NOT-A-TICKER") is None
    assert store.realised_for_ticker(None) is None
