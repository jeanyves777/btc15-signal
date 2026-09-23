"""The shared message surface, tested where it can drift back.

Every assertion here pins something a builder used to get wrong on its own:
a direction chip carrying an outcome, a gate named after its feature, a fill
promising a gross figure the settlement then paid net, two money snapshots in
one message, a rotation that moved on a message nobody received.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from btc15_signal import messages, surface  # noqa: E402
from btc15_signal.store import LifetimeRecord, MoneySnapshot, Store  # noqa: E402

NOW = 1_790_000_000_000
TICKER = "KXBTC15M-26SEP231100-00"

LIFETIME = LifetimeRecord(markets=157, winners=119, dollars=4.91,
                          since_ms=1_789_000_000_000, complete=False)
SNAP = MoneySnapshot(markets=19, winners=18, realised=3.74, open_mark=0.0,
                     taken_ms=NOW, has_mirror=True, lifetime=LIFETIME)

FACTS = [
    {"name": "Decision ask", "passed": True,
     "pass_text": "75c within 70-93c", "fail_text": ""},
    {"name": "BRTI distance", "passed": False,
     "pass_text": "", "fail_text": "4.0x - needs 10x"},
    {"name": "BRTI momentum", "passed": True,
     "pass_text": "+5.7 bps", "fail_text": ""},
    {"name": "Reference", "passed": True,
     "pass_text": "Kalshi BRTI, 900 pts, $85,946.05", "fail_text": "stale"},
]


def signal(**kw):
    args = dict(side="DOWN", ticker=TICKER, ask=0.75, remaining=528,
                confidence="HIGH", facts=FACTS, executable=False,
                status_line="no entry", snapshot=SNAP)
    args.update(kw)
    return messages.signal_message(**args)


# ------------------------------------------------------------- 1. order


def test_the_fixed_order_holds():
    text = signal(insight=surface.insight_line("signals",
                                               {"settled": 245, "wins": 200}))
    lines = text.splitlines()
    assert lines[0].startswith(surface.DOWN)          # 1 direction and event
    assert TICKER in lines[1]                          # 2 ticker
    assert "Ask" in lines[2]                           # 3 essential price
    checks_at = next(i for i, x in enumerate(lines) if "Price:" in x)
    divider_at = lines.index(surface.DIVIDER)
    assert checks_at < divider_at                      # 4 checks before money
    assert "no entry" in lines[divider_at - 1]         # 5 status last of all
    assert "Live" in lines[divider_at + 1]             # 7 money after divider
    assert lines[-1].startswith(surface.TARGET)        # 8 insight last
    assert lines.count(surface.DIVIDER) == 1           # exactly one divider


def test_the_full_ticker_is_preserved_for_traceability():
    assert TICKER in signal()


# ------------------------------------------------------------- 2. icons


def test_direction_and_outcome_are_separate_axes():
    """A profitable DOWN trade is 🔴⬇️ for direction and ✅💰 for result."""
    assert surface.side_icon("DOWN") == surface.DOWN
    assert surface.side_icon("UP") == surface.UP
    assert surface.result_icon(0.42, traded=True, won=True) == surface.WON_MONEY
    assert surface.result_icon(-1.7, traded=True, won=False) == surface.LOST_MONEY
    assert surface.result_icon(0.0, traded=True, won=True) == surface.FLAT_MONEY
    assert surface.result_icon(None, traded=False, won=True) == surface.WON_PAPER
    assert surface.result_icon(None, traded=False, won=False) == surface.LOST_PAPER
    # A profitable DOWN trade: direction chip and money chip, both, neither
    # standing in for the other.
    text = messages.result_message(
        side="DOWN", ticker=TICKER, winner="DOWN", won=True, traded=True,
        pnl=0.42, paid=0.78, snapshot=SNAP,
    )
    assert text.startswith(surface.WON_MONEY)
    assert surface.DOWN in text


# ------------------------------------------------------------ 3. checks


def test_every_check_is_shown_with_its_value_and_threshold():
    text = signal()
    assert "Price: 75¢ · range 70–93¢" in text
    assert "Distance: 4.0× · minimum 10×" in text
    assert "Momentum: +5.7 bps" in text
    # Passing AND failing, never only the failures.
    assert text.count(surface.PASS) >= 3
    assert surface.FAIL in text


def test_internal_feature_names_stay_out_of_the_face_of_the_message():
    text = signal()
    assert "BRTI distance" not in text
    assert "Decision ask" not in text
    assert "Distance:" in text and "Price:" in text


def test_the_reference_check_is_shortened_once_freshness_is_verified():
    text = signal()
    assert "Kalshi reference fresh" in text
    assert "900 pts" not in text            # sample counts belong in details
    stale = [dict(f) for f in FACTS]
    stale[3]["passed"] = False
    assert "Kalshi reference: stale" in signal(facts=stale)


def test_band_hold_progress_is_shown_when_it_applies():
    assert "Band held: 47s of 60s" in signal(band_hold=(47, 60))
    assert "Band held" not in signal()


def test_confidence_and_eligibility_stay_distinct():
    """A refused signal reading HIGH is correct, not a contradiction."""
    text = signal(confidence="HIGH", executable=False)
    assert "Confidence HIGH" in text
    assert "NOT EXECUTED" in text
    assert "Entry checks 3/4" in text
    # The confidence word must not appear in the header line.
    assert "HIGH" not in text.splitlines()[0]


# ------------------------------------------------------- 4. money footer


def test_the_money_footer_is_always_present_and_labelled_today():
    text = signal()
    assert "Live since" in text
    assert "+$4.91" in text
    assert "157 closed" in text and "119W–38L" in text
    assert "Today: +$3.74" in text
    assert "18W–1L" in text
    # "Today", not "Today (New York)" or "Today · NY".
    assert "New York" not in text and "NY" not in text


def test_the_footer_counts_executed_markets_not_signals():
    """`markets`/`winners` are settled markets we held a position in."""
    text = signal(insight=surface.insight_line("signals",
                                               {"settled": 245, "wins": 200}))
    assert "19 closed" in text          # executed
    assert "245 settled" in text        # signals, and clearly labelled
    assert "245 closed" not in text


def test_a_message_takes_one_snapshot(monkeypatch, tmp_path):
    """Two snapshots in one message are two instants the reader cannot tell
    apart. `report_settlement` used to build one, discard it, and build
    another milliseconds later."""
    store = Store(str(tmp_path / "t.db"))
    calls = []
    original = store.money_snapshot
    monkeypatch.setattr(
        store, "money_snapshot",
        lambda now_ms=None: (calls.append(now_ms), original(now_ms))[1],
    )
    from btc15_signal.config import Settings
    from btc15_signal.notify import Notifier

    notifier = Notifier(telegram=None, store=store, settings=Settings())
    notifier.snapshot(NOW)
    assert calls == [NOW], "the snapshot must be taken once, with an explicit instant"


# --------------------------------------------------------- 5. rotation


def test_rotation_is_stable_within_a_market_and_advances_on_delivery(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    first, second = NOW, NOW + 900_000
    a = store.insight_for(first, surface.INSIGHTS)
    store.assign_insight(first, a, NOW)
    assert store.insight_for(first, surface.INSIGHTS) == a
    # A second market before any result still shows the same variant.
    assert store.insight_for(second, surface.INSIGHTS) == a
    store.advance_insight(surface.INSIGHTS, NOW)
    b = store.insight_for(second, surface.INSIGHTS)
    assert b != a
    # ...and the first market keeps the one it was given.
    assert store.insight_for(first, surface.INSIGHTS) == a


def test_rotation_survives_a_restart(tmp_path):
    path = str(tmp_path / "t.db")
    store = Store(path)
    store.advance_insight(surface.INSIGHTS, NOW)
    expected = store.insight_for(NOW + 1, surface.INSIGHTS)
    assert Store(path).insight_for(NOW + 1, surface.INSIGHTS) == expected


def test_an_insight_with_no_data_is_skipped_not_invented():
    assert surface.insight_line("signals", {"settled": 0, "wins": 0}) == ""
    assert surface.insight_line("sessions", {"line": ""}) == ""
    assert surface.insight_line("paper", None) == ""
    assert surface.insight_line("nonsense", {"settled": 5}) == ""


def test_every_rotation_variant_renders():
    data = {
        "signals": {"settled": 245, "wins": 200},
        "paper": {"settled": 245, "net": -0.33, "basis": "1 contract"},
        "sessions": {"line": "US +$3.74"},
        "qualified": {"settled": 160, "wins": 131, "net": 1.42},
    }
    for variant in surface.INSIGHTS:
        assert surface.insight_line(variant, data[variant]), variant


# ------------------------------------------------- 6. priority outranks


def test_priority_rows_are_never_displaced_by_the_rotation():
    text = signal(
        priority=surface.priority_lines(
            partial="Partial fill: 1 of 2 contracts",
            pending="1 order still working",
            failure="Order rejected by the exchange"),
        insight=surface.insight_line("signals", {"settled": 245, "wins": 200}),
    )
    assert "Partial fill: 1 of 2 contracts" in text
    assert "1 order still working" in text
    assert "Order rejected by the exchange" in text
    assert "Signals:" in text          # the insight is still there too


def test_recovery_is_one_line_with_deficit_and_permission():
    class State:
        owes, deficit, base_only = True, 1.24, False

    line = surface.recovery_line(State())
    assert "$1.24 outstanding" in line
    assert "extra sizing allowed" in line
    State.base_only = True
    assert "base size only" in surface.recovery_line(State())
    State.owes = False
    assert surface.recovery_line(State()) == ""


# ------------------------------------------------------- 7. learning


def test_the_learning_notification_keeps_identifiers_out():
    text = messages.learning_update(markets=6475, confidence_changes=0,
                                    entry_changes=0)
    assert "LEARNING UPDATE" in text
    assert "Reviewed: 6,475 markets" in text
    assert "Confidence changes: None" in text
    assert "Entry-rule changes: None" in text
    assert "Current trading rules remain unchanged." in text
    assert "Automatic learning continues." in text
    # Policy ids, hashes, methods and splits belong in the log and /learning.
    for leak in ("kalshi-brti", "fingerprint", "arms-", "holdout", "brti-2"):
        assert leak not in text


def test_a_learning_change_names_the_setup_and_what_changed():
    text = messages.learning_update(
        markets=6475, confidence_changes=1, entry_changes=0,
        detail="Confidence raised on 10–15× distance setups priced 85–93¢.")
    assert "Confidence changes: 1" in text
    assert "10–15× distance setups priced 85–93¢" in text


# --------------------------------------------- 8. duplicate suppression


def test_an_event_is_delivered_once_even_across_a_restart(tmp_path):
    path = str(tmp_path / "t.db")
    store = Store(path)
    assert store.mark_delivered("settlement", "w1", NOW, 42, "body") is True
    assert store.mark_delivered("settlement", "w1", NOW, 43, "body") is False
    assert Store(path).delivered("settlement", "w1")["message_id"] == 42


def test_waiting_edits_in_place_and_preserves_the_buttons(tmp_path):
    """`editMessageText` drops the inline keyboard when `reply_markup` is
    omitted, so an edit that forgets the buttons silently removes the
    operator's execute button from a live signal."""
    import asyncio

    from btc15_signal.config import Settings
    from btc15_signal.notify import Notifier

    class FakeTelegram:
        def __init__(self):
            self.sent, self.edits = [], []

        async def send(self, text, buttons=None):
            self.sent.append((text, buttons))
            return 99

        async def edit(self, message_id, text, buttons=None):
            self.edits.append((message_id, text, buttons))
            return True

    store = Store(str(tmp_path / "t.db"))
    telegram = FakeTelegram()
    notifier = Notifier(telegram, store, Settings())
    buttons = [("Execute 1", "execute:abc")]

    async def drive():
        await notifier.update_status("signal", "w1", "held 20s", NOW, buttons)
        await notifier.update_status("signal", "w1", "held 40s", NOW, buttons)
        await notifier.update_status("signal", "w1", "held 40s", NOW, buttons)

    asyncio.run(drive())
    assert len(telegram.sent) == 1, "the first call sends, it does not edit"
    assert len(telegram.edits) == 1, "identical text must not be re-sent"
    assert telegram.edits[0][0] == 99
    assert telegram.edits[0][2] == buttons, "buttons must survive the edit"


# ------------------------------------------------- 9. financial wording


def test_maximum_profit_is_net_and_says_so():
    """The fill used to promise `contracts - cost` with no fee term while the
    settlement paid net - $0.20 against $0.19 on the same trade."""
    text = messages.fill_message(
        side="UP", ticker=TICKER, contracts=2, paid=0.84, fee=0.0189,
        remaining=540, confidence="HIGH", facts=FACTS, snapshot=SNAP)
    assert "Maximum net profit $0.30" in text
    # THE FEE IS A MONEY FIGURE, so it prints like one. This asserted
    # `$0.0189` - four decimals, a precision the account statement does not
    # have - while the total beside it was already rounded to cents, so one
    # line claimed more accuracy than the line it was explaining.
    assert "incl. $0.02 fees" in text
    assert "0.0189" not in text
    # The TOTAL still uses the unrounded fee: $1.68 + $0.0189 = $1.6989.
    assert "Cost $1.70" in text
    assert "estimated" not in text


def test_a_pending_fee_is_labelled_estimated():
    text = messages.fill_message(
        side="UP", ticker=TICKER, contracts=2, paid=0.84, fee=None,
        remaining=540, confidence="HIGH", facts=FACTS, snapshot=SNAP)
    assert "estimated" in text and "fee not yet reported" in text
    assert "before fees" in text


def test_an_untraded_signal_says_no_trade_and_zero():
    text = messages.result_message(
        side="UP", ticker=TICKER, winner="UP", won=True, traded=False,
        pnl=None, snapshot=SNAP)
    assert "Not traded · realised P&amp;L $0.00" in text
    assert "Took" not in text
    assert "SIGNAL WON · NOT TRADED" in text


def test_an_early_exit_says_already_counted_and_separates_money_from_call():
    text = messages.result_message(
        side="DOWN", ticker=TICKER, winner="UP", won=False, traded=True,
        pnl=0.2566, paid=0.86, exited_at=0.997, snapshot=SNAP)
    assert "Already counted at the sale." in text
    assert "DOWN prediction was wrong" in text      # the call
    assert "Realised <b>+$0.26</b>" in text         # the money
    assert text.startswith(surface.WON_MONEY)       # profitable despite a miss


def test_a_price_is_not_rounded_into_a_price_that_never_traded():
    assert surface.cents(0.997) == "99.7¢"
    assert surface.cents(0.84) == "84¢"
    assert surface.cents(0.7) == "70¢"


def test_money_carries_its_currency_and_sign():
    assert surface._signed_dollars(4.91) == "+$4.91"
    assert surface._signed_dollars(-0.85) == "−$0.85"


# ------------------------------------------------ shortening without lying

def test_a_short_reason_is_left_alone():
    assert surface.clipped("policy refused on load") == "policy refused on load"


def test_a_long_reason_stops_on_a_word_boundary():
    """A hard slice stopped at `...(current arms-nested-1)); it w`, and a
    reader cannot tell a truncated line from a corrupted record."""
    text = ("policy refused on load (fitted by superseded method "
            "arms-calibrated-2 (current arms-nested-1)); it was withdrawn")
    out = surface.clipped(text)
    assert out.endswith("…")
    assert not out.rstrip("…").endswith(" ")
    assert " w…" not in out, "still cutting mid-word"
    assert out.rstrip("…") in text, "it must not invent characters"


def test_a_single_long_word_is_still_shortened():
    """No boundary to stop on is not a reason to print the whole thing."""
    out = surface.clipped("x" * 400, limit=50)
    assert len(out) <= 51
    assert out.endswith("…")
