"""Recovery SIZING ends before the deficit is repaid, on the operator's rule.

    End recovery sizing when BOTH are met:
      * four profitable, fully closed market positions since the cycle began
      * at least 50% of the cycle's INITIAL deficit recovered, net of fees and
        subsequent realised losses

It is an exposure-reduction rule, not a claim that a loss becomes more likely
after four wins. Nothing here predicts anything; it caps how long the account
carries doubled size.

The line these tests defend hardest: ending sizing must NOT erase the deficit
or announce a full recovery. The money is still missing and the ledger has to
keep saying so.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import messages, recovery_exit  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

NOW = 1_790_000_000_000


# ---------------------------------------------------- the rule, in isolation

def test_both_conditions_are_required():
    """AND, not OR. Either alone leaves the upsize on."""
    assert not recovery_exit.decide(initial=2.00, deficit=1.40, wins=4)
    assert not recovery_exit.decide(initial=2.00, deficit=0.80, wins=3)
    assert recovery_exit.decide(initial=2.00, deficit=0.80, wins=4)


def test_the_boundaries_are_inclusive():
    """Exactly four wins and exactly 50% must fire - a rule that needs a
    fraction more than it states is not the rule that was specified."""
    assert recovery_exit.decide(initial=2.00, deficit=1.00, wins=4)
    assert not recovery_exit.decide(initial=2.00, deficit=1.0001, wins=4)
    assert not recovery_exit.decide(initial=2.00, deficit=1.00, wins=3)


def test_the_fraction_is_configurable_inside_the_operators_range():
    assert recovery_exit.decide(initial=2.00, deficit=1.15, wins=4,
                                exit_fraction=0.40)
    assert not recovery_exit.decide(initial=2.00, deficit=1.15, wins=4,
                                    exit_fraction=0.60)
    assert recovery_exit.validate_fraction(0.40) == 0.40
    assert recovery_exit.validate_fraction(0.60) == 0.60


def test_a_fraction_outside_the_range_is_refused_not_clamped():
    """A threshold nobody intended is worse than an error."""
    import pytest

    for bad in (0.10, 0.39, 0.61, 0.95):
        with pytest.raises(ValueError):
            recovery_exit.validate_fraction(bad)


def test_progress_is_net_of_subsequent_losses():
    """The denominator is the cycle's OPENING deficit. A later loss raises
    what is outstanding and so lowers the percentage."""
    assert recovery_exit.recovered_fraction(initial=4.00, deficit=2.00) == 0.5
    assert recovery_exit.recovered_fraction(initial=4.00, deficit=5.00) == 0.0
    assert recovery_exit.recovered_fraction(initial=0.0, deficit=1.0) == 0.0


def test_nothing_owed_is_not_an_early_end():
    """Full recovery closes the cycle on its own path. Reporting it as an
    early end would confuse repaid with stood down."""
    d = recovery_exit.decide(initial=2.00, deficit=0.0, wins=9)
    assert not d.end_sizing and d.reason == ""


def test_it_can_be_switched_off():
    d = recovery_exit.decide(initial=2.0, deficit=0.5, wins=9, enabled=False)
    assert not d.end_sizing and "disabled" in d.reason


# ------------------------------------------------------------- the ledger

def realise(store, rows, start_ms=NOW):
    """Realise (ticker, amount) pairs, net of fees as the ledger records."""
    for i, (ticker, amount) in enumerate(rows):
        store.db.execute(
            "INSERT OR IGNORE INTO realised_events (event_id, ticker, amount, "
            "realised_ms, source, recorded_ms) VALUES (?,?,?,?,?,?)",
            (f"{ticker}:{i}:{amount}", ticker, amount, start_ms + i * 1000,
             "settlement", start_ms + i * 1000),
        )
    store.db.commit()
    return store.apply_realised_to_deficit(start_ms + 60_000)


def wins4(each=0.30):
    return [(f"W{i}", each) for i in range(4)]


def test_a_loss_opens_a_cycle(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [("LOSS", -2.00)])
    assert state.deficit == 2.00 and state.initial == 2.00
    assert state.active and state.cycle_id
    assert state.wins == 0


def test_four_wins_and_half_back_ends_sizing_but_keeps_the_deficit(tmp_path):
    """THE CENTRAL GUARANTEE."""
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [("LOSS", -2.00), *wins4(each=0.25)])
    assert state.wins == 4
    assert state.recovered_fraction == 0.5
    assert state.base_only, "both conditions met"
    assert state.active is False, "active means MAY UPSIZE"
    assert state.deficit == 1.00, "and 1.00 is still missing"
    assert state.owes, "the account is still down; the books must say so"


def test_four_wins_short_of_half_keeps_sizing_on(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [("LOSS", -2.00), *wins4(each=0.20)])
    assert state.wins == 4 and abs(state.recovered_fraction - 0.4) < 1e-9
    assert not state.base_only and state.active


def test_half_back_on_three_markets_keeps_sizing_on(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [("LOSS", -2.00), ("A", 0.40), ("B", 0.40),
                            ("C", 0.40)])
    assert state.wins == 3 and state.recovered_fraction >= 0.5
    assert not state.base_only and state.active


# ----------------------------------------------- counting markets, not fills

def test_a_market_counts_once_however_many_times_it_settles(tmp_path):
    """Base and add-on on one ticker are ONE position with one outcome.
    Counting events would reach four on two markets."""
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [
        ("LOSS", -2.00),
        ("A", 0.30), ("A", 0.30),
        ("B", 0.30), ("B", 0.30),
    ])
    assert state.wins == 2, "two markets, four fills"
    assert not state.base_only


def test_re_folding_the_same_events_changes_nothing(tmp_path):
    """The service folds every poll. A second pass over events already
    applied must not move the deficit or the count."""
    store = Store(str(tmp_path / "t.db"))
    realise(store, [("LOSS", -2.00), *wins4(each=0.10)])
    before = store.stored_deficit()
    store.apply_realised_to_deficit(NOW + 70_000)
    store.apply_realised_to_deficit(NOW + 80_000)
    after = store.stored_deficit()
    assert after.wins == before.wins == 4
    assert abs(after.deficit - before.deficit) < 1e-9


def test_a_duplicate_event_id_is_rejected_outright(tmp_path):
    """A re-synced settlement arriving under the same id is ignored, so a
    market cannot pay down the deficit twice."""
    store = Store(str(tmp_path / "t.db"))
    realise(store, [("LOSS", -2.00), ("A", 0.50)])
    before = store.stored_deficit()
    store.db.execute(
        "INSERT OR IGNORE INTO realised_events (event_id, ticker, amount, "
        "realised_ms, source, recorded_ms) VALUES (?,?,?,?,?,?)",
        ("A:1:0.5", "A", 0.50, NOW + 1000, "settlement", NOW + 1000),
    )
    store.db.commit()
    after = store.apply_realised_to_deficit(NOW + 70_000)
    assert abs(after.deficit - before.deficit) < 1e-9
    assert after.wins == before.wins


def test_wins_need_not_be_consecutive(tmp_path):
    """A loss in between does not reset the counter."""
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [
        ("LOSS", -2.00),
        ("A", 0.40), ("B", 0.40),
        ("MID", -0.20),
        ("C", 0.40), ("D", 0.40),
    ])
    assert state.wins == 4, "the loss did not reset the count"
    assert abs(state.deficit - 0.60) < 1e-9
    assert state.base_only


def test_an_intervening_loss_lowers_the_measured_progress(tmp_path):
    """"Net of subsequent realised losses" - the loss counts against the
    percentage, it is not ignored because it came after the wins."""
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [
        ("LOSS", -2.00), ("A", 0.30), ("B", 0.30), ("C", 0.30),
        ("BIG", -0.80),
        ("D", 0.30),
    ])
    assert state.wins == 4
    assert state.recovered_fraction < 0.5
    assert not state.base_only, "four wins alone is not enough"
    assert state.active


# ------------------------------------------ base-only phase, no reactivation

def test_a_loss_in_the_base_only_phase_does_not_reactivate_sizing(tmp_path):
    """The loop this rule exists to break."""
    store = Store(str(tmp_path / "t.db"))
    realise(store, [("LOSS", -2.00), *wins4(each=0.25)])
    state = realise(store, [("NEW", -0.60)], start_ms=NOW + 100_000)
    assert abs(state.deficit - 1.60) < 1e-9, "the loss is recorded in full"
    assert state.base_only and not state.active, "but sizing stays off"


def test_a_loss_in_the_base_only_phase_does_not_reset_the_win_counter(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    realise(store, [("LOSS", -2.00), *wins4(each=0.25)])
    state = realise(store, [("NEW", -0.60)], start_ms=NOW + 100_000)
    assert state.wins == 4


def test_base_size_profits_clear_the_remainder(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    realise(store, [("LOSS", -2.00), *wins4(each=0.25)])
    state = realise(store, [("E", 0.60)], start_ms=NOW + 100_000)
    assert state.base_only
    assert abs(state.deficit - 0.40) < 1e-9


def test_reaching_zero_closes_the_cycle_and_a_later_loss_opens_a_new_one(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    realise(store, [("LOSS", -2.00), *wins4(each=0.25)])
    state = realise(store, [("E", 1.50)], start_ms=NOW + 100_000)
    assert state.deficit == 0.0
    assert not state.base_only and not state.active
    assert state.wins == 0 and state.cycle_id == "", "the cycle is closed"
    fresh = realise(store, [("LOSS2", -1.00)], start_ms=NOW + 200_000)
    assert fresh.active and fresh.initial == 1.00
    assert fresh.wins == 0, "a new cycle starts its own count"


def test_full_recovery_ends_sizing_immediately_even_before_four_wins(tmp_path):
    """Existing termination is unchanged: nothing left to size for."""
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [("LOSS", -2.00), ("A", 2.50)])
    assert state.deficit == 0.0 and not state.active
    assert state.wins == 0 and not state.base_only


# ------------------------------------------------------------- persistence

def test_the_cycle_survives_a_restart(tmp_path):
    path = str(tmp_path / "t.db")
    store = Store(path)
    realise(store, [("LOSS", -2.00), *wins4(each=0.25)])
    cycle = store.stored_deficit().cycle_id
    store.db.close()

    reopened = Store(path)
    state = reopened.stored_deficit()
    assert state.base_only and not state.active
    assert abs(state.deficit - 1.00) < 1e-9
    assert state.initial == 2.00 and state.wins == 4
    assert state.cycle_id == cycle, "the same cycle, not a new one"


def test_a_restart_cannot_hand_back_the_upsize(tmp_path):
    path = str(tmp_path / "t.db")
    store = Store(path)
    realise(store, [("LOSS", -2.00), *wins4(each=0.25)])
    store.db.close()
    assert not Store(path).recovery_is_active()


# ------------------------------------------------------- the resting order

def test_a_resting_add_is_cancelled_when_sizing_ends():
    """And the reason does not claim the money came back."""
    from btc15_signal.recovery_add import AddLimits, should_cancel

    cancel, reason = should_cancel(
        features=None, entry_side="UP", crossed_since_entry=False,
        remaining_s=400, recovery_active=False, recovery_owes=True,
        base_position_open=True, limits=AddLimits(),
    )
    assert cancel
    assert "sizing ended" in reason and "outstanding" in reason
    assert "completed" not in reason


def test_a_genuinely_completed_recovery_still_says_completed():
    from btc15_signal.recovery_add import AddLimits, should_cancel

    cancel, reason = should_cancel(
        features=None, entry_side="UP", crossed_since_entry=False,
        remaining_s=400, recovery_active=False, recovery_owes=False,
        base_position_open=True, limits=AddLimits(),
    )
    assert cancel and reason == "recovery completed"


def test_a_fill_is_banked_before_any_cancellation_is_considered():
    """The cancel/fill race: `_maintain` checks for a fill FIRST, so an order
    that filled while we were deciding to pull it is banked, not lost."""
    import inspect

    from btc15_signal.recovery_add_runner import RecoveryAddRunner

    source = inspect.getsource(RecoveryAddRunner._maintain)
    assert source.index("_bank_if_filled") < source.index("should_cancel")


def test_the_add_on_refuses_to_place_once_sizing_has_ended():
    """Behavioural, not a source scan: an ended cycle is refused even when
    every other condition would have allowed the add."""
    from btc15_signal.recovery_add import AddLimits, evaluate
    from btc15_signal.validation import kalshi_fee_charged

    decision = evaluate(
        features=None, entry_side="DOWN", entry_fill=0.75, current_ask=0.73,
        crossed_since_entry=False, remaining_s=500, required_per_trade=0.25,
        recovery_active=False, already_added=False, open_exposure=0.0,
        limits=AddLimits(), fee=kalshi_fee_charged,
    )
    assert not decision.place
    assert "not active" in decision.reason


# --------------------------------------------------------------- reporting

def test_the_transition_message_uses_the_real_counts(tmp_path):
    """"4 winning trades - 50% recovered" printed on a cycle that reached
    five wins and 63% would be a template, not a report."""
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [("LOSS", -2.00), ("A", 0.35), ("B", 0.35),
                            ("C", 0.35), ("D", 0.35)])
    text = messages.recovery_size_ended(state)
    assert "RECOVERY SIZE ENDED" in text
    assert f"{state.wins} winning trades" in text
    assert f"{state.recovered_fraction:.0%} recovered" in text
    assert f"${state.deficit:,.2f}" in text
    assert "Continuing at normal base size." in text
    assert "$0.00" not in text, "it must not read as a full recovery"


def test_the_transition_fires_once_and_is_not_called_cleared(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    realise(store, [("LOSS", -2.00)])
    assert store.recovery_transition(NOW)[0] == "armed"
    realise(store, wins4(each=0.25), start_ms=NOW + 100_000)
    event, state = store.recovery_transition(NOW + 110_000)
    assert event == "size_ended", "not 'cleared' - money is still owed"
    assert state.owes
    assert store.recovery_transition(NOW + 120_000)[0] is None, "once only"


def test_a_genuine_repayment_still_reports_cleared(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    realise(store, [("LOSS", -2.00)])
    store.recovery_transition(NOW)
    realise(store, wins4(each=0.25), start_ms=NOW + 100_000)
    store.recovery_transition(NOW + 110_000)
    realise(store, [("E", 1.20)], start_ms=NOW + 200_000)
    event, state = store.recovery_transition(NOW + 210_000)
    assert event == "cleared" and state.deficit == 0.0


def test_the_status_line_keeps_showing_the_debt(tmp_path):
    """Going silent would read as "paid back"."""
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [("LOSS", -2.00), *wins4(each=0.25)])
    line = messages.recovery_line(state)
    assert "size ended" in line.lower()
    assert "1.00" in line and "4 winning" in line


def test_the_thresholds_come_from_settings(tmp_path):
    """A knob that does nothing is how an operator ends up believing they
    turned something off."""
    from btc15_signal.config import Settings

    store = Store(str(tmp_path / "t.db"))
    settings = Settings()
    settings.recovery_partial_exit_enabled = False
    store.configure_recovery_exit(settings)
    state = realise(store, [("LOSS", -2.00), *wins4(each=0.25)])
    assert not state.base_only and state.active


def test_the_deployed_default_is_fifty_percent_and_four_wins():
    from btc15_signal.config import Settings

    settings = Settings()
    assert settings.recovery_exit_fraction == 0.50
    assert settings.recovery_exit_required_wins == 4
    assert settings.recovery_partial_exit_enabled is True


# ------------------------------------- four is a floor, not a maximum

def test_sizing_continues_past_four_wins_while_under_half():
    """No four-win cap. Both conditions must hold, so a long run that has
    not moved the money keeps the upsize on - past the fourth win, past the
    fortieth. Requiring BOTH therefore stops sizing LESS often than either
    alone: more restrictive about stopping, not about exposure."""
    for wins in (4, 10, 40, 89):
        assert not recovery_exit.decide(initial=8.68, deficit=6.00, wins=wins)
    assert recovery_exit.decide(initial=8.68, deficit=4.00, wins=89)


def test_a_long_grind_under_half_keeps_sizing_on(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    rows = [("LOSS", -4.00)] + [(f"M{i}", 0.10) for i in range(12)]
    state = realise(store, rows)
    assert state.wins == 12, "twelve winning markets"
    assert abs(state.recovered_fraction - 0.3) < 1e-9
    assert not state.base_only and state.active, "still under half"


# ----------------------------------- a migrated baseline is not the loss

def test_a_deficit_already_in_flight_is_flagged_as_seeded(tmp_path):
    """`initial` adopted at migration is a STARTING POINT, not the original
    loss, and the percentage measured against it is not progress against
    that loss. The distinction has to survive in the data."""
    store = Store(str(tmp_path / "t.db"))
    # A deficit written before the cycle columns existed: no initial, no id.
    store.db.execute(
        "INSERT INTO recovery_deficit (id, deficit, markets, opened_ms, "
        "updated_ms, steps) VALUES (1, 1.40, 3, ?, ?, 4)", (NOW, NOW),
    )
    store.db.commit()
    state = store.apply_realised_to_deficit(NOW + 1000)
    assert state.seeded, "adopted, not observed"
    assert state.initial == 1.40
    assert state.cycle_id


def test_a_cycle_opened_by_an_observed_loss_is_not_seeded(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [("LOSS", -2.00)])
    assert not state.seeded, "this IS the original hole"


def test_the_seeded_flag_survives_a_restart(tmp_path):
    path = str(tmp_path / "t.db")
    store = Store(path)
    store.db.execute(
        "INSERT INTO recovery_deficit (id, deficit, markets, opened_ms, "
        "updated_ms, steps) VALUES (1, 1.40, 3, ?, ?, 4)", (NOW, NOW),
    )
    store.db.commit()
    store.apply_realised_to_deficit(NOW + 1000)
    store.db.close()
    assert Store(path).stored_deficit().seeded


def test_a_seeded_cycle_says_so_in_the_message(tmp_path):
    """Printing the percentage bare would claim more than it knows."""
    store = Store(str(tmp_path / "t.db"))
    store.db.execute(
        "INSERT INTO recovery_deficit (id, deficit, markets, opened_ms, "
        "updated_ms, steps) VALUES (1, 2.00, 0, ?, ?, 4)", (NOW, NOW),
    )
    store.db.commit()
    store.apply_realised_to_deficit(NOW + 1000)
    state = realise(store, wins4(each=0.25), start_ms=NOW + 10_000)
    assert state.seeded and state.base_only
    text = messages.recovery_size_ended(state)
    assert "carried-over balance" in text


def test_a_genuine_cycle_does_not_carry_the_caveat(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    state = realise(store, [("LOSS", -2.00), *wins4(each=0.25)])
    assert not state.seeded
    assert "carried-over" not in messages.recovery_size_ended(state)


def test_clearing_resets_the_seeded_flag(tmp_path):
    """A fresh cycle opened by a real loss is not a migration artefact."""
    store = Store(str(tmp_path / "t.db"))
    store.db.execute(
        "INSERT INTO recovery_deficit (id, deficit, markets, opened_ms, "
        "updated_ms, steps) VALUES (1, 1.00, 0, ?, ?, 4)", (NOW, NOW),
    )
    store.db.commit()
    store.apply_realised_to_deficit(NOW + 1000)
    assert store.stored_deficit().seeded
    realise(store, [("A", 1.50)], start_ms=NOW + 10_000)
    fresh = realise(store, [("LOSS", -1.00)], start_ms=NOW + 20_000)
    assert not fresh.seeded
