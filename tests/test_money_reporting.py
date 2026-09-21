"""Every dollar figure the bot reports must describe money that actually moved.

The bot reported -$3.91 to Telegram on a night whose real loss was $0.85: it
priced nine signals, eight of which were never traded, at ten contracts each
while the account had traded one. These tests pin the distinction.
"""

from btc15_signal.store import Store


def traded(store, window, price, won, count=1, fee=None, status="filled"):
    """A settled prediction with a real order behind it."""
    store.record((window, 1, 81_000.0, 80_900.0, "UP", 8, 0.9, "T", price, 1))
    proposal = store.create_proposal(
        "primary", window, "T", "UP", price, 0.0, count, 9999, 9999, 1
    )
    store.db.execute(
        "UPDATE trade_proposals SET status=?, fee_paid=? WHERE id=?",
        (status, fee, proposal.id),
    )
    store.db.execute(
        "UPDATE predictions SET won=? WHERE window_open=?", (1 if won else 0, window)
    )
    store.db.commit()
    return proposal


def paper(store, window, price, won):
    """A settled signal nobody ever placed an order for."""
    store.record((window, 1, 81_000.0, 80_900.0, "UP", 8, 0.9, "T", price, 1))
    store.db.execute(
        "UPDATE predictions SET won=? WHERE window_open=?", (1 if won else 0, window)
    )
    store.db.commit()


def test_paper_signals_are_not_money(tmp_path):
    """The defect that started this: eight untraded signals priced as P&L."""
    store = Store(str(tmp_path / "m.db"))
    for i in range(8):
        paper(store, 1000 + i, 0.90, won=True)

    assert store.scoreboard(contracts=1)[0] == 8  # the signals are still scored
    trades, wins, dollars = store.realised_record()
    assert (trades, wins, dollars) == (0, 0, 0.0)  # but none of it is money


def test_real_money_uses_the_size_actually_filled(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    traded(store, 1000, 0.84, won=False, count=1, fee=0.0095)

    trades, wins, dollars = store.realised_record()
    assert (trades, wins) == (1, 0)
    assert abs(dollars - -0.8495) < 1e-9

    # The old hypothetical sizing is an order of magnitude away.
    assert abs(store.scoreboard(contracts=10)[2]) > 8


def test_the_stored_fee_beats_the_modelled_one(tmp_path):
    """What Kalshi charged, not what the formula predicts."""
    store = Store(str(tmp_path / "m.db"))
    traded(store, 1000, 0.84, won=True, count=1, fee=0.05)  # implausible, but recorded
    assert abs(store.realised_record()[2] - (0.16 - 0.05)) < 1e-9


def test_a_missing_fee_falls_back_to_the_charged_model_not_to_zero(tmp_path):
    from btc15_signal.validation import kalshi_fee_charged

    store = Store(str(tmp_path / "m.db"))
    traded(store, 1000, 0.84, won=True, count=1, fee=None)
    expected = 0.16 - kalshi_fee_charged(0.84, 1)
    assert abs(store.realised_record()[2] - expected) < 1e-9
    assert kalshi_fee_charged(0.84, 1) > 0  # and not silently free


def test_an_early_exit_is_money_at_its_exit_price(tmp_path):
    """Scoring it by who eventually won would credit back a loss already taken."""
    store = Store(str(tmp_path / "m.db"))
    proposal = traded(store, 1000, 0.90, won=True, count=10, fee=0.0)
    store.mark_exited(proposal.id, 0.40, 10, "sold")

    trades, wins, dollars = store.realised_record()
    assert (trades, wins) == (1, 0)  # sold below cost: a loss, whoever won
    assert -5.7 < dollars < -4.9  # about -$5, not the +$1 settlement would show


def test_an_exit_above_cost_counts_as_a_win(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    proposal = traded(store, 1000, 0.40, won=False, count=10, fee=0.0)
    store.mark_exited(proposal.id, 0.90, 10, "sold")

    trades, wins, dollars = store.realised_record()
    assert (trades, wins) == (1, 1)
    assert dollars > 4.0


def test_a_trade_is_counted_once_whether_held_or_exited(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    traded(store, 1000, 0.84, won=True, count=1, fee=0.0)
    proposal = traded(store, 2000, 0.84, won=True, count=1, fee=0.0)
    store.mark_exited(proposal.id, 0.90, 1, "sold")

    assert store.realised_record()[0] == 2  # not 3, and not 1


def test_unfilled_and_rejected_orders_are_not_money(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    for window, status in ((1000, "rejected"), (2000, "unfilled"), (3000, "pending")):
        traded(store, window, 0.84, won=False, count=1, fee=None, status=status)

    assert store.realised_record() == (0, 0, 0.0)


def test_the_two_records_answer_different_questions(tmp_path):
    """Signal accuracy and account P&L must not be conflated into one number."""
    store = Store(str(tmp_path / "m.db"))
    paper(store, 1000, 0.90, won=True)
    paper(store, 2000, 0.90, won=True)
    traded(store, 3000, 0.90, won=False, count=1, fee=0.0)

    settled, wins, _ = store.scoreboard(contracts=1)
    assert (settled, wins) == (3, 2)  # two of three signals were right

    trades, live_wins, dollars = store.realised_record()
    assert (trades, live_wins) == (1, 0)  # the one we traded was not
    assert dollars < 0


# ------------------------------------------- the reporting surface as a whole


def test_the_header_never_merges_paper_and_real_money():
    """The defect: one dollar figure covering trades nobody made, at a size
    nobody chose. Two labelled figures, never one."""
    from btc15_signal import messages

    text = messages.scoreboard(9, 7, -0.33, live=(1, 0, -0.85))
    assert "paper" in text
    assert "Live" in text
    assert "-0.33" in text and "-0.85" in text
    # and the signal line no longer carries a bare, unlabelled dollar amount
    first_line = text.splitlines()[0]
    assert "$" not in first_line


def test_the_header_says_so_when_nothing_has_been_traded():
    from btc15_signal import messages

    text = messages.scoreboard(9, 7, -0.33, live=(0, 0, 0.0))
    assert "no trades placed yet" in text
    assert "-0.00" not in text  # never a money figure implying a real loss


def test_the_paper_basis_defaults_to_the_size_the_bot_orders():
    """It defaulted to ten contracts, left over from paper testing."""
    from btc15_signal.config import Settings
    from btc15_signal.main import report_sizing

    sizing, basis = report_sizing(Settings())
    assert sizing == {"contracts": 1.0}
    assert basis == "1 contract"


def test_every_message_builds_its_header_the_same_way():
    """There were eight copies of the expression, so one stale default was
    wrong in eight places at once."""
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    # exactly one construction site, inside head_for
    assert source.count("messages.scoreboard(") == 1
    assert source.count("head_for(store, settings)") >= 7


def test_the_dashboard_and_telegram_agree_about_real_money(tmp_path):
    """Two surfaces priced the same rows three different ways."""
    from btc15_signal.dashboard import load, summarise
    from btc15_signal.store import Store

    path = str(tmp_path / "agree.db")
    store = Store(path)
    traded(store, 1000, 0.84, won=False, count=1, fee=0.0095)
    paper(store, 2000, 0.90, won=True)

    _, _, telegram_money = store.realised_record()
    stats = summarise(load(path, 1.0))
    assert abs(stats["real_net"] - telegram_money) < 1e-9
    assert stats["real_trades"] == 1  # not 2 - one was never traded


def test_the_loss_floor_and_the_settlement_report_agree_on_an_exit(tmp_path):
    """They previously stated opposite signs for the same trade."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "sign.db"))
    now = 1_700_000_000_000
    day = now - (now % 86_400_000) + 3_600_000
    store.record((day, 1, 81_000.0, 80_900.0, "UP", 8, 0.9, "T", 0.90, 1))
    proposal = store.create_proposal(
        "primary", day, "T", "UP", 0.90, 0.0, 1, 9999, day + 9e5, day
    )
    store.db.execute("UPDATE trade_proposals SET status='filled', fill_price=0.90")
    store.db.execute("UPDATE predictions SET won=1 WHERE window_open=?", (day,))
    store.db.commit()
    store.mark_exited(proposal.id, 0.35, 1, "sold")

    floor_money = store.auto_state(day + 60_000)[3]
    _, _, record_money = store.realised_record()
    trade = store.trade_for_window(day)

    assert floor_money < 0 and record_money < 0  # both a loss...
    assert abs(floor_money - record_money) < 1e-9  # ...and the same loss
    assert trade["exited"] is True  # so settlement reports the sale, not the win


def test_an_exit_is_priced_off_the_fill_not_the_posted_limit(tmp_path):
    """A buy limit only ever fills at or below itself, so using the limit is
    biased loss-wards every time - by more than the edge being traded."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "fill.db"))
    now = 1_700_000_000_000
    day = now - (now % 86_400_000) + 3_600_000
    proposal = store.create_proposal(
        "primary", day, "T", "UP", 0.87, 0.0, 1, 9999, day + 9e5, day
    )
    store.db.execute("UPDATE trade_proposals SET status='filled'")
    store.db.commit()
    store.record_fill(proposal.id, day, 0.84, 1, 0.0095)  # filled 3c better
    store.mark_exited(proposal.id, 0.35, 1, "sold")

    realised = store.auto_state(day + 60_000)[3]
    against_limit = 1 * (0.35 - 0.87)
    against_fill = 1 * (0.35 - 0.84)
    assert realised > against_limit  # not scored at the 87c limit
    assert abs(realised - against_fill) < 0.05  # scored at the 84c fill


# ------------------------------------------------- partially filled exits


def test_a_partial_exit_keeps_the_unsold_contracts_on_the_books(tmp_path):
    """The audit's case: 22 bought at 90c, 10 sold at 50c, 12 expire worthless.

    Scoring only the sold leg left the account $15.11 down while the daily floor
    read $4.24 and kept trading - and the open-position guard was cleared while
    twelve contracts were still at risk.
    """
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "p.db"))
    now = 1_700_000_000_000
    day = now - (now % 86_400_000) + 3_600_000
    store.record((day, 1, 81_000.0, 80_900.0, "UP", 8, 0.9, "T", 0.90, 1))
    proposal = store.create_proposal(
        "primary", day, "T", "UP", 0.90, 0.0, 22, 9999, day + 9e5, day
    )
    store.db.execute("UPDATE trade_proposals SET status='filled', fee_paid=0.0")
    store.db.commit()

    store.mark_exited(proposal.id, 0.50, 10, "sold 10 of 22")

    # Still open: twelve contracts have not been sold.
    assert store.auto_state(day + 60_000)[4] == 1
    assert store.open_position_detail(day) is not None

    store.db.execute("UPDATE predictions SET won=0 WHERE window_open=?", (day,))
    store.db.commit()

    realised = store.auto_state(day + 60_000)[3]
    sold_leg = 10 * (0.50 - 0.90)          # -4.00
    held_leg = 12 * (0.0 - 0.90)           # -10.80
    assert abs(realised - (sold_leg + held_leg)) < 0.2
    assert realised < -14  # not the -4.24 that let trading continue


def test_a_partial_exit_trips_the_daily_floor_it_used_to_hide_from(tmp_path):
    from btc15_signal.autotrade import AutoLimits, AutoState, auto_block_reason
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "p.db"))
    now = 1_700_000_000_000
    day = now - (now % 86_400_000) + 3_600_000
    store.record((day, 1, 81_000.0, 80_900.0, "UP", 8, 0.9, "T", 0.90, 1))
    proposal = store.create_proposal(
        "primary", day, "T", "UP", 0.90, 0.0, 22, 9999, day + 9e5, day
    )
    store.db.execute("UPDATE trade_proposals SET status='filled', fee_paid=0.0")
    store.db.execute("UPDATE predictions SET won=0 WHERE window_open=?", (day,))
    store.db.commit()
    store.mark_exited(proposal.id, 0.50, 10, "partial")

    limits = AutoLimits(10.0, 40, 6, 120, 1.0)
    state = AutoState(*store.auto_state(day + 60_000))
    assert "daily loss limit" in auto_block_reason(limits, state, 0.9, enabled=True)


def test_a_full_exit_does_close_the_position(tmp_path):
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "p.db"))
    proposal = store.create_proposal("primary", 1000, "T", "UP", 0.90, 0.0, 5, 9999, 9999, 1)
    store.db.execute("UPDATE trade_proposals SET status='filled'")
    store.db.commit()
    store.mark_exited(proposal.id, 0.50, 5, "sold all")

    assert store.open_position_detail(1000) is None
    status = store.db.execute(
        "SELECT status FROM trade_proposals WHERE id=?", (proposal.id,)
    ).fetchone()[0]
    assert status == "exited"


def test_an_open_position_has_no_realised_figure_yet(tmp_path):
    """A trade still at risk must not be counted as money made or lost."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "p.db"))
    store.record((1000, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.90, 1))  # won stays NULL
    store.create_proposal("primary", 1000, "T", "UP", 0.90, 0.0, 1, 9999, 9999, 1)
    store.db.execute("UPDATE trade_proposals SET status='filled'")
    store.db.commit()

    assert store.realised_record() == (0, 0, 0.0)


def test_all_three_accounting_paths_share_one_implementation():
    """They each had their own copy and disagreed about the same trade."""
    from pathlib import Path

    store_src = Path("src/btc15_signal/store.py").read_text(encoding="utf-8")
    dash_src = Path("src/btc15_signal/dashboard.py").read_text(encoding="utf-8")
    assert store_src.count("def position_pnl(") == 1
    assert store_src.count("position_pnl(") >= 3  # def + auto_state + realised_record
    assert "position_pnl(" in dash_src


# ------------------------------------ the money the user sets, and is shown


def test_the_execute_button_honours_the_manual_size_even_when_the_rule_agrees():
    """It sized from the AUTO budget whenever the rule qualified, so /size was
    silently ignored on exactly the signals most worth pressing."""
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    assert "budget_for(store, settings, auto=qualified)" not in source
    assert "budget_for(store, settings, auto=False), contract_ask" in source


def test_a_sub_contract_budget_says_it_will_overspend(tmp_path):
    """contracts_for_budget floors at one contract, so $0.50 buys a 90c contract."""
    import time

    from btc15_signal.config import Settings
    from btc15_signal.main import handle_size_command
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "s.db"))
    reply = handle_size_command(store, Settings(), "/size 0.50", int(time.time() * 1000))
    assert "may be spent" in reply
    assert "0.45" in reply  # the size of the overspend, not just a vague warning

    quiet = handle_size_command(store, Settings(), "/size 5", int(time.time() * 1000))
    assert "may be spent" not in quiet


def test_a_hand_pressed_order_records_its_fill_like_an_automatic_one():
    """Only the auto path read fills back, so a pressed trade was booked at the
    quoted ask - and fed that price to the daily loss floor."""
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    assert source.count("async def record_fill_detail(") == 1
    # both the unattended block and the button handler call it
    assert source.count("await record_fill_detail(") == 2


def test_an_unconfirmed_fill_is_never_presented_as_the_price_paid():
    """A buy limit only ever fills at or below itself, so showing the limit
    silently overstates the cost."""
    from btc15_signal import messages

    for text in (
        messages.auto_filled(
            head="H", ticker="T", side="UP", price=0.87, count=1,
            note="n", limit=0.87, exact=False,
        ),
        messages.order_result(
            status="filled", side="UP", count=1, note="n", order_id=None,
            price=0.87, limit=0.87, exact=False,
        ),
    ):
        assert "unconfirmed" in text

    confident = messages.auto_filled(
        head="H", ticker="T", side="UP", price=0.84, count=1, note="n", limit=0.87
    )
    assert "unconfirmed" not in confident


def test_the_button_confirmation_states_the_money():
    """Pressing Execute committed real cash and the reply never said how much."""
    from btc15_signal import messages

    text = messages.order_result(
        status="protected", side="UP", count=2, note="n", order_id="x",
        price=0.84, limit=0.87, fee=0.02,
    )
    assert "cost $1.68" in text
    assert "84%" in text
    assert "Fee $0.02" in text


def test_price_improvement_is_labelled_per_contract_and_in_total():
    """At one contract the two coincide, which is how a whole-order figure came
    to be labelled as a per-contract one."""
    from btc15_signal import messages

    text = messages.auto_filled(
        head="H", ticker="T", side="UP", price=0.84, count=11, note="n", limit=0.87
    )
    assert "3.0c/contract better" in text
    assert "$0.33 total" in text


def test_the_auto_status_money_says_which_period_it_covers():
    """An unlabelled today-figure beside an all-time one read as a contradiction."""
    from btc15_signal import messages
    from btc15_signal.autotrade import AutoLimits, AutoState

    text = messages.auto_status(
        head=messages.scoreboard(9, 7, -0.33, live=(1, 0, -0.85)),
        on=True,
        budget=1.0,
        limits=AutoLimits(10.0, 40, 6, 120, 1.0),
        state=AutoState(1, 1, 9e9, -0.85, 0),
        blocked="",
    )
    assert "realised today" in text
    assert "Live:" in text  # the all-time figure, distinctly labelled


# ------------------------- defects found while verifying the earlier fixes


def test_a_reporting_failure_cannot_erase_a_real_position():
    """The try block spanned the order AND the Telegram send, so a network
    hiccup after a real fill rewrote the position to 'failed' - removing it
    from the daily loss floor and freeing the one-position guard while the
    contracts were still live."""
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    auto = source.split("---- unattended execution")[1]
    auto = auto[: auto.index("    head = head_for(store, settings)")]

    # The call is now multi-line (it carries the ceiling), so match the
    # call itself rather than its first argument.
    order_call = auto.index("await trader.execute_with_take_profit(")
    mark_failed = auto.index('"failed"')
    send = auto.index("messages.auto_filled(")
    # The failure handler sits between the order and the reporting, so only the
    # order call can reach it.
    assert order_call < mark_failed < send
    assert "post-order reporting failed" in auto


def test_an_unfilled_order_is_never_announced_as_a_purchase():
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    assert "AUTO ORDER NOT FILLED" in source
    assert "nothing was spent" in source
    # and the count is no longer faked up from the requested size
    assert "result.filled_count or count" not in source


def test_every_status_that_holds_contracts_is_accounted(tmp_path):
    """execute_with_take_profit returns 'protected'/'unprotected' too, and a
    position under either was invisible to the loss floor."""
    from btc15_signal.store import HELD_STATUSES, Store

    assert set(HELD_STATUSES) == {"filled", "protected", "unprotected"}
    for status in HELD_STATUSES:
        store = Store(str(tmp_path / f"{status}.db"))
        store.record((1000, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.84, 1))
        proposal = store.create_proposal(
            "primary", 1000, "T", "UP", 0.84, 0.0, 1, 9999, 9999, 1
        )
        store.db.execute(
            "UPDATE trade_proposals SET status=?, fill_price=0.84, fee_paid=0.0095 WHERE id=?",
            (status, proposal.id),
        )
        store.db.execute("UPDATE predictions SET won=0 WHERE window_open=1000")
        store.db.commit()
        trades, _, dollars = store.realised_record()
        assert trades == 1, f"{status} was invisible to the account record"
        assert abs(dollars - -0.8495) < 1e-9, status


def test_the_live_line_survives_an_empty_signal_record():
    """Returning early dropped it exactly when a first real trade is all there is."""
    from btc15_signal import messages

    text = messages.scoreboard(0, 0, 0.0, live=(1, 0, -0.85))
    assert "Live: -0.85" in text
    assert "No settled signals" in text  # both facts, not one replacing the other


def test_the_paper_line_states_the_basis_it_was_actually_given():
    """It said "per contract" whatever report_basis was, so a payout basis
    labelled a ten-contract figure as a one-contract one."""
    from btc15_signal import messages

    assert "on 1 contract" in messages.scoreboard(9, 7, -0.24, basis="1 contract")
    assert "on $10 max payout" in messages.scoreboard(9, 7, -3.91, basis="$10 max payout")


def test_a_partly_sold_position_is_reported_as_partly_sold(tmp_path):
    """Branching on "exited" alone reported it as wholly held, erasing the sale
    and flipping the verdict's sign."""
    from btc15_signal.store import Store, position_pnl

    store = Store(str(tmp_path / "ps.db"))
    proposal = store.create_proposal(
        "primary", 1000, "T", "UP", 0.90, 0.0, 22, 9999, 9999, 1
    )
    store.db.execute(
        "UPDATE trade_proposals SET status='filled', fill_price=0.90, fee_paid=0.0"
    )
    store.db.commit()
    store.mark_exited(proposal.id, 0.50, 10, "partial")

    trade = store.trade_for_window(1000)
    assert trade["exit_count"] == 10 and trade["count"] == 22
    pnl = position_pnl(
        paid=trade["paid"], count=trade["count"], entry_fee=trade["fee"],
        exit_price=trade["exit_price"], exit_count=trade["exit_count"], won=False,
    )
    assert -15.1 < pnl < -14.7  # both legs: -4.00 sold, -10.80 held
    assert store.open_position_detail(1000) is not None  # guard still held


def test_an_open_position_price_is_the_fill_not_the_limit(tmp_path):
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "op.db"))
    proposal = store.create_proposal("primary", 1000, "T", "UP", 0.87, 0.0, 1, 9999, 9999, 1)
    store.db.execute("UPDATE trade_proposals SET status='filled'")
    store.db.commit()
    store.record_fill(proposal.id, 1000, 0.84, 1, 0.0095)
    assert store.open_position_detail(1000)[1] == 0.84  # not the 0.87 limit


def test_the_dashboard_spend_multiplies_price_by_contracts(tmp_path):
    """Summing price alone reported $0.84 spent on a position costing $8.40."""
    from btc15_signal.dashboard import load, summarise
    from btc15_signal.store import Store

    path = str(tmp_path / "spend.db")
    store = Store(path)
    traded(store, 1000, 0.84, won=False, count=10, fee=0.0)
    stats = summarise(load(path, 1.0))
    assert abs(stats["real_staked"] - 8.40) < 1e-6


def test_an_unconfirmed_fill_is_flagged_on_the_settlement_report_too(tmp_path):
    """Only the order confirmation said "unconfirmed"; the settlement report
    presented the same estimate as settled fact, and so did the loss floor."""
    from btc15_signal import messages
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "u.db"))
    store.record((1000, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.87, 1))
    store.create_proposal("primary", 1000, "T", "UP", 0.87, 0.0, 1, 9999, 9999, 1)
    store.db.execute("UPDATE trade_proposals SET status='filled'")  # no fill_price
    store.db.commit()

    trade = store.trade_for_window(1000)
    assert trade["confirmed"] is False
    assert trade["paid"] == 0.87  # the limit, standing in

    text = messages.settlement(
        head="H", ticker="T", side="UP", winner="DOWN", won=False, target=1.0,
        contract_price=0.87, pnl=-0.88, qualified=True, basis="1 contract",
        contracts=1, exact=False,
    )
    assert "posted limit" in text
    assert "this or lower" in text


def test_a_confirmed_fill_carries_no_caveat(tmp_path):
    from btc15_signal import messages
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "c.db"))
    store.record((1000, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.87, 1))
    proposal = store.create_proposal("primary", 1000, "T", "UP", 0.87, 0.0, 1, 9999, 9999, 1)
    store.db.execute("UPDATE trade_proposals SET status='filled'")
    store.db.commit()
    store.record_fill(proposal.id, 1000, 0.84, 1, 0.0095)

    assert store.trade_for_window(1000)["confirmed"] is True
    text = messages.settlement(
        head="H", ticker="T", side="UP", winner="DOWN", won=False, target=1.0,
        contract_price=0.84, pnl=-0.85, qualified=True, basis="1 contract", contracts=1,
    )
    assert "posted limit" not in text


def test_a_reversion_fill_cannot_overwrite_the_primary_signals_price(tmp_path):
    """predictions is keyed by window_open alone and written only by the primary
    path, so an unqualified UPDATE let a reversion order rewrite the primary
    signal's recorded price - and every scoreboard figure derives from it."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "x.db"))
    store.record((1000, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.90, 1))
    reversion = store.create_proposal(
        "reversion", 1000, "T", "UP", 0.32, 0.50, 1, 9999, 9999, 1
    )
    store.db.execute("UPDATE trade_proposals SET status='filled' WHERE id=?", (reversion.id,))
    store.db.commit()

    store.record_fill(reversion.id, 1000, 0.31, 1, 0.01)

    price = store.db.execute(
        "SELECT contract_price FROM predictions WHERE window_open=1000"
    ).fetchone()[0]
    assert price == 0.90  # the primary signal's price, untouched


# ------------------------------------------- cashing out, and session tracking


def test_cash_out_only_fires_when_the_profit_is_nearly_all_in():
    """Measured on 5,753 paired trades: banking 90% of the available profit
    costs -$0.0006/contract against holding, which is noise. Cashing out
    earlier is not free - -$0.0075 at a 0.95 bid, -$0.0127 at 0.90."""
    from btc15_signal.config import Settings

    s = Settings()
    assert s.cash_out_enabled
    assert s.cash_out_capture >= 0.85  # never the expensive early thresholds
    assert s.cash_out_min_bid >= 0.85

    # Bought at 0.74: the most it can make is 0.26, so 90% of that is 0.234,
    # i.e. it must be bid at least 0.974 before anything is banked.
    paid = 0.74
    needed = paid + s.cash_out_capture * (1.0 - paid)
    assert needed > 0.97


def test_cash_out_can_never_sell_at_a_loss():
    """The gate is a fraction of the profit ABOVE the entry, so a position under
    water cannot qualify however far it falls."""
    from btc15_signal.config import Settings

    s = Settings()
    paid = 0.80
    for bid in (0.05, 0.40, 0.79, 0.80):
        available = 1.0 - paid
        assert (bid - paid) < s.cash_out_capture * available


def test_the_cash_out_message_states_what_was_given_up_too():
    """Banking 23c while forgoing 3c is the whole trade-off; showing only the
    gain makes the rule look better than it measured."""
    from btc15_signal import messages

    text = messages.cash_out(
        head="H", ticker="T", side="UP", paid=0.74, bid=0.97, count=1,
        captured=0.885, remaining=240, note="Sold 1 at 97%", sold=True,
    )
    assert "Banked" in text and "+0.21" in text  # net of both fees
    assert "Gave up" in text and "$0.03" in text
    assert "88%" in text


def test_sessions_are_derived_not_stored(tmp_path):
    """From window_open, so the split applies to every signal ever recorded
    without a migration or a backfill."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "s.db"))
    # 2026-09-21 03:00 UTC -> asia;  13:00 UTC -> us
    for window, won in ((1789952400000, 1), (1789988400000, 0)):
        store.record((window, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.90, 1))
        store.db.execute("UPDATE predictions SET won=? WHERE window_open=?", (won, window))
    store.db.commit()

    buckets = store.by_session()
    assert sum(b["signals"] for k, b in buckets.items() if k in
               {"asia", "europe", "us", "late-us"}) == 2
    hours = [k for k in buckets if k.endswith(":00")]
    assert len(hours) == 2  # one bucket per distinct hour


def test_the_session_view_never_gates_a_trade():
    """Session may scale SIZE; it must never refuse a trade.

    Section 4 banned session data from the trade path because two of five
    intervals straddle zero and filtering on that fits the sample rather than
    the market. That reasoning is about REMOVING trades: a filter fitted to
    noise destroys real opportunity permanently, while a bounded multiplier
    only sizes some trades slightly wrong.

    So the ban is now on the property that actually matters - session can
    never produce a refusal - rather than on the mention of a table. The
    reporting view stays out of the order path either way.
    """
    from pathlib import Path

    from btc15_signal.regime import MIN_WEIGHT, weight_for_hour

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    assert "MEASURED_SESSION_EDGE" in source
    auto = source.split("---- unattended execution")[1]
    auto = auto[: auto.index("    head = head_for(store, settings)")]
    # The reporting table and view remain display-only.
    assert "MEASURED_SESSION_EDGE" not in auto
    assert "by_session" not in auto
    # And the weight cannot starve an order: every hour stays clear of zero.
    assert MIN_WEIGHT > 0
    assert all(weight_for_hour(h).weight >= MIN_WEIGHT for h in range(24))
    # It must not appear in the refusal logic at all.
    blocked = auto[: auto.index("if blocked:")]
    assert "auto_block_reason" in blocked
    assert "regime" not in blocked.split("auto_block_reason")[0]


def test_the_cash_out_figure_is_net_like_every_other_money_figure():
    """It reported gross, so a cash-out said "+0.27" and the settlement four
    minutes later said "+0.25" for the same trade - which reads as a running
    total that has stopped updating."""
    from btc15_signal import messages
    from btc15_signal.validation import kalshi_fee_charged

    text = messages.cash_out(
        head="H", ticker="T", side="UP", paid=0.72, bid=0.987, count=1,
        captured=0.93, remaining=232, note="Sold 1 at 99%", sold=True,
    )
    net = (0.987 - 0.72) - kalshi_fee_charged(0.72, 1) - kalshi_fee_charged(0.987, 1)
    assert f"{net:+,.2f}" in text
    assert "+0.27" not in text  # the gross figure


def test_the_two_messages_about_one_trade_agree_on_the_money(tmp_path):
    """A cash-out and its settlement recap describe the same dollars."""
    from btc15_signal import messages
    from btc15_signal.store import Store, position_pnl

    store = Store(str(tmp_path / "agree.db"))
    store.record((1000, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.72, 1))
    proposal = store.create_proposal("primary", 1000, "T", "UP", 0.72, 0.0, 1, 9999, 9999, 1)
    store.db.execute("UPDATE trade_proposals SET status='filled', fill_price=0.72, fee_paid=0.0142")
    store.db.commit()
    store.mark_exited(proposal.id, 0.987, 1, "Sold 1 at 99%")

    trade = store.trade_for_window(1000)
    booked = position_pnl(
        paid=trade["paid"], count=trade["count"], entry_fee=trade["fee"],
        exit_price=trade["exit_price"], exit_count=trade["exit_count"], won=True,
    )
    text = messages.cash_out(
        head="H", ticker="T", side="UP", paid=0.72, bid=0.987, count=1,
        captured=0.93, remaining=232, note="n", sold=True,
    )
    # Within a cent: the message models the entry fee, the store has the real one.
    shown = float(text.split("Banked <b>")[1].split("</b>")[0])
    assert abs(shown - booked) < 0.01


def test_a_settlement_recap_says_the_money_was_already_counted():
    """Otherwise a second message carrying an unchanged total reads as a total
    that has stopped updating."""
    from btc15_signal import messages

    recap = messages.settlement(
        head="H", ticker="T", side="UP", winner="UP", won=True, target=1.0,
        contract_price=0.72, pnl=0.25, qualified=True, basis="1 sold at 99%",
        contracts=1, exited_at=0.987,
    )
    assert "Already counted when it sold" in recap

    fresh = messages.settlement(
        head="H", ticker="T", side="UP", winner="UP", won=True, target=1.0,
        contract_price=0.84, pnl=0.15, qualified=True, basis="1 contract", contracts=1,
    )
    assert "Already counted" not in fresh


# ---------------------------------------- studying what the rule turned down


def test_a_rejected_signal_records_which_gate_rejected_it(tmp_path):
    """Recording only that something said no is a dead end: you can see the
    market went our way anyway, but not which gate cost you."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "g.db"))
    store.record((1000, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.65, 0, "price band, momentum"))
    store.record((2000, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.72, 0, "price band"))
    store.record((3000, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.88, 1, None))
    store.db.execute("UPDATE predictions SET won=1")
    store.db.commit()

    study = store.gate_study()
    assert study["price band"]["n"] == 2
    assert study["price band"]["sole"] == 1  # only one was rejected by it alone
    assert study["momentum"]["n"] == 1
    assert study["momentum"]["sole"] == 0  # never the only objection
    assert study["(taken by the rule)"]["n"] == 1


def test_the_gate_study_reports_edge_not_win_rate(tmp_path):
    """Most favourites win. A 95c contract winning 89% of the time is a loss,
    and a win rate cannot show that."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "e.db"))
    for i in range(9):
        store.record((1000 + i, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.95, 0, "price band"))
    store.db.execute("UPDATE predictions SET won=1")
    store.db.execute("UPDATE predictions SET won=0 WHERE window_open=1000")
    store.db.commit()

    g = store.gate_study()["price band"]
    assert g["wins"] / g["n"] > 0.85  # a healthy-looking win rate...
    assert g["edge"] / g["n"] < 0  # ...on a losing trade


def test_a_thin_sample_is_marked_as_unjudgeable():
    from btc15_signal import messages

    text = messages.intel(
        head="H",
        gates={"price band": {"n": 19, "wins": 17, "edge": 2.4, "sole": 19}},
        needed=6000,
    )
    assert "⚪" in text  # not a green light on 19 samples
    assert "6,000" in text
    assert "too few to judge" in text


def test_a_large_sample_earns_a_verdict():
    from btc15_signal import messages

    text = messages.intel(
        head="H",
        gates={"price band": {"n": 9000, "wins": 8000, "edge": 90.0, "sole": 9000}},
        needed=6000,
    )
    assert "\U0001f7e2" in text  # green: positive edge on enough samples


def test_samples_needed_scales_with_the_price_actually_traded(tmp_path):
    """Binary variance is p(1-p), which collapses at favourite prices - the one
    thing working in our favour on sample size."""
    from btc15_signal.main import samples_needed
    from btc15_signal.store import Store

    cheap = Store(str(tmp_path / "c.db"))
    cheap.record((1, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.50, 1, None))
    cheap.db.execute("UPDATE predictions SET won=1")
    cheap.db.commit()

    dear = Store(str(tmp_path / "d.db"))
    dear.record((1, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.95, 1, None))
    dear.db.execute("UPDATE predictions SET won=1")
    dear.db.commit()

    assert samples_needed(dear) < samples_needed(cheap)


# ------------------------------- the research archive, for strategies we lack


def test_archiving_sits_outside_every_trading_gate():
    """The first archive lived inside primary_signal, so it saw only the
    630s-330s entry scan and skipped any poll where the spread was wide - which
    threw away both the path from entry to expiry and precisely the conditions
    worth studying. A strategy we do not yet have will not be found in the
    minutes we already trade."""
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")

    # Called from the loop, before any trading function.
    loop = source.split("while True:")[1]
    assert loop.index("archive_observation(") < loop.index("await primary_signal(")

    # And the archiver itself applies no entry-window or spread gate. Sliced to
    # THIS function's body - the next top-level def - rather than to
    # primary_signal, because helpers now sit between the two and a wider slice
    # tests whatever happens to have been added there.
    body = source.split("def archive_observation(")[1]
    tail = body.split(chr(10))
    cut = next(
        (i for i, ln in enumerate(tail[1:], 1)
         if ln.startswith("def ") or ln.startswith("async def ")),
        len(tail),
    )
    body = chr(10).join(tail[:cut])
    assert "entry_to_seconds" not in body
    assert "max_spread_bps" not in body
    assert "store.observe_full(" in body


def test_archiving_can_never_stop_the_service_trading():
    """Data collection is strictly subordinate to placing orders."""
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    body = source.split("def archive_observation(")[1]
    body = body[: body.index("async def primary_signal(")]
    assert "except Exception" in body
    assert "archive failed" in body


def test_an_observation_carries_the_whole_feature_vector(tmp_path):
    """A rule can only be fitted later on features that were saved at the time."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "o.db"))
    cols = {r[1] for r in store.db.execute("PRAGMA table_info(observations)")}
    for needed in (
        "momentum_5m_bps", "volatility_5m_bps", "normalized_distance", "spread_bps",
        "futures_basis_bps", "taker_imbalance", "window_high", "window_low",
        "raw_probability", "our_ask", "yes_ask", "no_ask", "remaining_s",
        "rule_match", "failed_gates", "won", "final_price",
    ):
        assert needed in cols, needed


def test_observations_settle_with_the_trade_and_cannot_drift(tmp_path):
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "o.db"))
    for remaining in (600, 500, 400):
        store.observe((
            1000, remaining, 1, "T", 84000.0, 84100.0, "UP", 0.9, 8,
            0.85, 0.85, 0.16, 5.0, 10.0, 12.0, 1.2, 1.0, 2.0, 0.1,
            84200.0, 83900.0, 8.0, 1, None,
        ))
    coverage = store.observation_coverage()
    assert coverage["rows"] == 3
    assert coverage["settled"] == 0
    assert coverage["windows"] == 1

    assert store.settle_observations(1000, "UP", 84150.0) == 3
    assert store.observation_coverage()["settled"] == 3
    # Settling twice must not rewrite an outcome.
    assert store.settle_observations(1000, "DOWN", 1.0) == 0


def test_the_same_poll_is_never_archived_twice(tmp_path):
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "o.db"))
    row = (1000, 600, 1, "T", 1.0, 1.0, "UP", 0.9, 8, 0.85, 0.85, 0.16,
           5.0, 10.0, 12.0, 1.2, 1.0, 2.0, 0.1, 1.0, 1.0, 8.0, 1, None)
    store.observe(row)
    store.observe(row)
    assert store.observation_coverage()["rows"] == 1


def test_record_still_accepts_the_older_ten_field_tuple(tmp_path):
    """An older build passing the pre-failed_gates tuple must still record the
    signal rather than fail - that class of break once killed the service."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "r.db"))
    assert store.record((1000, 1, 1.0, 1.0, "UP", 8, 0.9, "T", 0.85, 1))
    assert store.db.execute(
        "SELECT failed_gates FROM predictions WHERE window_open=1000"
    ).fetchone()[0] is None


# ------------------ do not present noise as information


def test_no_edge_is_claimed_at_a_price_nobody_measured():
    """The commentary said "+0.0006 expected edge" about a 65c contract by
    applying the 0.85-0.93 band figure to a price outside it. Unknown is not
    zero, and it is certainly not a number."""
    from btc15_signal.main import measured_edge_at

    for unmeasured in (0.50, 0.65, 0.68, 0.80, 0.84, 0.95, 0.99):
        assert measured_edge_at(unmeasured) is None, unmeasured
    assert measured_edge_at(0.86) == 0.0177
    assert measured_edge_at(0.91) == 0.0114


def test_an_unmeasured_price_says_so_rather_than_quoting_a_figure():
    from btc15_signal.decision import decision_facts

    facts = decision_facts(
        ticker="T", remaining_s=600, side="UP", btc=85_046.0, target=84_935.0,
        our_ask=0.65, exit_bid=0.64, yes_bid=0.64, yes_ask=0.65, no_ask=0.36,
        yes_levels=[(0.64, 3000.0)], no_levels=[(0.35, 1000.0)],
        momentum_5m_bps=0.8, volatility_5m_bps=3.0, futures_basis_bps=1.0,
        taker_imbalance=0.1, spread_bps=1.0, session="us", vol_regime="low",
        book_age_s=8.0, rule_match=False, failed_gates="contract price band",
        holding=False, entry_paid=None, unrealised=None,
        model_probability=0.98, measured_edge=None, slippage=0.01,
    )
    assert facts["economics"]["price_was_measured"] is False
    assert facts["economics"]["edge_after_fees"] is None
    assert facts["economics"]["worth_doing"] is False
    joined = " ".join(facts["summary"]["evidence"])
    assert "no edge has ever been measured" in joined


def test_a_hair_above_zero_is_not_worth_having():
    """`> 0` announced +0.0006 - six hundredths of a cent - as worth having."""
    from btc15_signal.decision import decision_facts

    def edge_words(measured, price):
        facts = decision_facts(
            ticker="T", remaining_s=600, side="UP", btc=1.0, target=0.9,
            our_ask=price, exit_bid=price - 0.01, yes_bid=price - 0.01,
            yes_ask=price, no_ask=1 - price, yes_levels=[(price, 100.0)],
            no_levels=[(1 - price, 100.0)], momentum_5m_bps=1.0,
            volatility_5m_bps=5.0, futures_basis_bps=0.0, taker_imbalance=0.0,
            spread_bps=1.0, session="us", vol_regime="low", book_age_s=1.0,
            rule_match=True, failed_gates="", holding=False, entry_paid=None,
            unrealised=None, model_probability=0.9, measured_edge=measured,
            slippage=0.01,
        )
        return " ".join(facts["summary"]["evidence"]), facts["economics"]["worth_doing"]

    thin_words, thin_ok = edge_words(0.0140, 0.86)   # ~+0.0055 after fee
    fat_words, fat_ok = edge_words(0.0177, 0.86)     # ~+0.0092 after fee
    assert fat_ok and "worth having" in fat_words

    barely_words, barely_ok = edge_words(0.0090, 0.86)  # ~+0.0005 after fee
    assert not barely_ok
    assert "too thin to be worth having" in barely_words


def test_an_observed_rate_is_suppressed_below_a_usable_sample():
    """"observed 100% (1 samples)" read as corroboration directly beneath a
    model reading of 71%. It was one coin flip."""
    from btc15_signal.messages import _calibration

    thin = _calibration(0.71, 1.0, 1)
    assert "100%" not in thin
    assert "too few settled signals" in thin

    solid = _calibration(0.98, 0.87, 46)
    assert "observed 87%" in solid and "46 samples" in solid

    # And the model figure is never called a probability.
    assert "Model score" in thin and "Model score" in solid


def test_the_settled_trade_carries_the_side_it_actually_held(tmp_path):
    """`settle_observations` scores the realised figure on the side HELD, and
    read `trade["side"]` from a dict that never had the key - so every
    settlement following a fill raised KeyError and the watchdog restarted the
    service. It crashed at 17:00, 17:15, 17:45 and 18:00 on 2026-09-21 before
    the missing column was noticed."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "s.db"))
    store.create_proposal("primary", 1000, "T", "DOWN", 0.85, 0.0, 1, 9999, 9999, 1)
    store.db.execute(
        "UPDATE trade_proposals SET status='filled', fill_price=0.84, fee_paid=0.0095"
    )
    store.db.commit()

    trade = store.trade_for_window(1000)
    assert trade["side"] == "DOWN", "the held side must survive the round trip"

    store.observe((
        1000, 600, 1, "T", 84000.0, 84100.0, "DOWN", 0.9, 8,
        0.85, 0.85, 0.16, 5.0, 10.0, 12.0, 1.2, 1.0, 2.0, 0.1,
        84200.0, 83900.0, 8.0, 1, None,
    ))
    # The call that used to raise.
    assert store.settle_observations(1000, "DOWN", 84_150.0) == 1
