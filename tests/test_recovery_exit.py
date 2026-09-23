"""Recovery stands down early, and standing down is not the same as repaid.

The operator's rule, and it is a requirement rather than a proposal:

    "Although we trigger the recovery after a certain set of wins - one, two,
     three, four wins - we should not stay at a recovery size, because there
     is a likelihood of a losing trade to come in and that will set us back.
     Even after a 50% recovery of the initial loss, turn off recovery. That's
     enough, because we've seen that even regular size is able to recover on
     its own."

The failure mode being prevented: late in a recovery the remaining deficit is
small but the position is still double size, so one loss more than undoes the
run of wins that got there - and arms recovery again, deeper. Recover, lose
bigger, recover. The upsize is most dangerous exactly where it looks nearly
finished.

The line these tests defend hardest is that standing down must NOT zero the
deficit. The money is still missing; writing it off would make the ledger lie
about the account.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import recovery_exit  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

NOW = 1_790_000_000_000


# ------------------------------------------------------------ the rule

def test_halfway_is_enough_on_its_own():
    d = recovery_exit.decide(peak=2.00, deficit=1.00, wins=1)
    assert d.stand_down
    assert "50%" in d.reason


def test_just_under_halfway_is_not_enough():
    d = recovery_exit.decide(peak=2.00, deficit=1.02, wins=1)
    assert not d.stand_down


def test_four_wins_lowers_the_bar_to_forty_percent():
    """A long grind of small wins is the state in which the next loss hurts
    most: many trades have gone by with the upsize riding all of them."""
    assert not recovery_exit.decide(peak=2.00, deficit=1.25, wins=3).stand_down
    d = recovery_exit.decide(peak=2.00, deficit=1.20, wins=4)
    assert d.stand_down
    assert "4 wins" in d.reason


def test_four_wins_does_not_fire_below_forty_percent():
    """Patience is not a licence: four wins that barely moved the deficit
    leave the upsize on, because there is still real ground to make up."""
    assert not recovery_exit.decide(peak=2.00, deficit=1.70, wins=4).stand_down


def test_nothing_owed_is_not_a_stand_down():
    """Cleared and stood-down are different states and must not share a
    message - one means the money came back, the other that it did not."""
    d = recovery_exit.decide(peak=2.00, deficit=0.0, wins=9)
    assert not d.stand_down and d.reason == ""


def test_progress_is_measured_against_the_PEAK_not_the_opening_deficit():
    """A loss part-way through deepens the hole. Judging against the original
    figure would make the percentage jump on arithmetic rather than money."""
    assert recovery_exit.recovered_fraction(peak=4.00, deficit=2.00) == 0.5
    # Same dollars owed, deeper hole dug -> less of it recovered.
    assert recovery_exit.recovered_fraction(peak=8.00, deficit=2.00) == 0.75
    assert recovery_exit.recovered_fraction(peak=0.0, deficit=1.0) == 0.0


def test_it_can_be_switched_off():
    d = recovery_exit.decide(peak=2.0, deficit=0.5, wins=9, enabled=False)
    assert not d.stand_down and "disabled" in d.reason


# ------------------------------------------- standing down is not repaying

def fold(store, amounts, start_ms=NOW):
    """Realise a sequence of P&L amounts, one market each."""
    for i, amount in enumerate(amounts):
        store.db.execute(
            "INSERT INTO realised_events (event_id, ticker, amount, "
            "realised_ms, source, recorded_ms) VALUES (?,?,?,?,?,?)",
            (f"e{i}-{amount}", f"T{i}", amount, start_ms + i * 1000,
             "settlement", start_ms + i * 1000),
        )
    store.db.commit()
    return store.apply_realised_to_deficit(start_ms + 10_000)


def test_a_loss_arms_recovery(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    state = fold(store, [-2.00])
    assert state.deficit == 2.00
    assert state.active, "recovery arms on a realised loss"
    assert state.peak == 2.00


def test_recovery_stands_down_at_halfway_but_still_owes(tmp_path):
    """THE CENTRAL GUARANTEE. The upsize stops; the debt does not vanish."""
    store = Store(str(tmp_path / "t.db"))
    state = fold(store, [-2.00, 1.10])
    assert state.stood_down, "50% back is enough"
    assert state.active is False, "active means MAY UPSIZE"
    assert abs(state.deficit - 0.90) < 1e-9, "and 0.90 is still missing"
    assert state.owes, "the account is still down; the books must say so"


def test_a_stood_down_epoch_keeps_reducing_on_base_size_wins(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    fold(store, [-2.00, 1.10])
    state = fold(store, [0.40], start_ms=NOW + 100_000)
    assert state.stood_down
    assert abs(state.deficit - 0.50) < 1e-9


def test_clearing_the_deficit_resets_the_stand_down(tmp_path):
    """Once the money is genuinely back the epoch ends, so a future loss arms
    recovery again normally. Otherwise recovery would be off forever."""
    store = Store(str(tmp_path / "t.db"))
    fold(store, [-2.00, 1.10])
    state = fold(store, [0.95], start_ms=NOW + 100_000)
    assert state.deficit == 0.0
    assert not state.stood_down and not state.active
    later = fold(store, [-1.00], start_ms=NOW + 200_000)
    assert later.active, "a new loss arms recovery again"
    assert later.peak == 1.00, "and the peak is the new hole, not the old one"


def test_once_down_it_stays_down_for_the_epoch(tmp_path):
    """Re-arming on the next loss is the loop this exists to break."""
    store = Store(str(tmp_path / "t.db"))
    fold(store, [-2.00, 1.10])              # stood down at 0.90 owed
    state = fold(store, [-0.60], start_ms=NOW + 100_000)
    assert state.deficit == 1.50, "the loss is still recorded in full"
    assert state.stood_down and not state.active, "but the upsize stays off"


def test_four_small_wins_stand_recovery_down(tmp_path):
    """The patience branch, end to end: a grind that has not reached halfway."""
    store = Store(str(tmp_path / "t.db"))
    state = fold(store, [-2.00, 0.22, 0.22, 0.22, 0.22])
    assert state.wins == 4
    assert 0.40 <= state.recovered_fraction < 0.50
    assert state.stood_down, "four wins and >=40% back is enough"


def test_three_wins_short_of_halfway_keeps_recovery_on(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    state = fold(store, [-2.00, 0.22, 0.22, 0.22])
    assert state.wins == 3 and not state.stood_down
    assert state.active, "still upsizing; the bar has not been met either way"


def test_a_deeper_hole_moves_the_goalposts(tmp_path):
    """Peak is a high-water mark. A fresh loss must not make the SAME dollars
    owed look like more progress than before."""
    store = Store(str(tmp_path / "t.db"))
    fold(store, [-1.00])
    state = fold(store, [-3.00], start_ms=NOW + 100_000)
    assert state.peak == 4.00
    assert state.recovered_fraction == 0.0


def test_the_state_survives_a_restart(tmp_path):
    path = str(tmp_path / "t.db")
    store = Store(path)
    fold(store, [-2.00, 1.10])
    store.db.close()
    reopened = Store(path)
    state = reopened.stored_deficit()
    assert state.stood_down and abs(state.deficit - 0.90) < 1e-9
    assert state.peak == 2.00 and state.wins == 1
    assert not state.active, "a restart cannot hand back the upsize"


# --------------------------------------------- it is visible and honest

def test_the_stand_down_announcement_does_not_claim_the_money_is_back(tmp_path):
    """CLEARED says "deficit back to $0.00". Sending that on a stand-down
    would tell the operator the account had recovered when it had not."""
    from btc15_signal import messages

    store = Store(str(tmp_path / "t.db"))
    state = fold(store, [-2.00, 1.10])
    text = messages.recovery_stood_down(state, store.money_snapshot(NOW))
    assert "STOOD DOWN" in text
    assert "still outstanding" in text
    assert "0.90" in text
    assert "$0.00" not in text


def test_the_transition_reports_stood_down_not_cleared(tmp_path):
    """`active` going False now means either repaid OR stood down, and the
    two must not share an event name."""
    store = Store(str(tmp_path / "t.db"))
    fold(store, [-2.00])
    event, _ = store.recovery_transition(NOW)
    assert event == "armed"
    fold(store, [1.10], start_ms=NOW + 100_000)
    event, state = store.recovery_transition(NOW + 110_000)
    assert event == "stood_down", "not 'cleared' - money is still owed"
    assert state.owes


def test_a_genuine_repayment_still_reports_cleared(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    fold(store, [-2.00])
    store.recovery_transition(NOW)
    fold(store, [1.10], start_ms=NOW + 100_000)
    store.recovery_transition(NOW + 110_000)          # stood_down
    fold(store, [0.95], start_ms=NOW + 200_000)
    event, state = store.recovery_transition(NOW + 210_000)
    assert event == "cleared" and state.deficit == 0.0


def test_the_status_line_keeps_showing_the_debt_after_standing_down(tmp_path):
    """Going silent would read as "paid back"."""
    from btc15_signal import messages

    store = Store(str(tmp_path / "t.db"))
    state = fold(store, [-2.00, 1.10])
    line = messages.recovery_line(state)
    assert "stood down" in line.lower()
    assert "0.90" in line


def test_the_thresholds_come_from_settings(tmp_path):
    """A knob that does nothing is how an operator ends up believing they
    turned something off."""
    from btc15_signal.config import Settings

    store = Store(str(tmp_path / "t.db"))
    settings = Settings()
    settings.recovery_partial_exit_enabled = False
    store.configure_recovery_exit(settings)
    state = fold(store, [-2.00, 1.10])
    assert not state.stood_down, "disabled means disabled"
    assert state.active, "recovery keeps upsizing when the exit is off"
