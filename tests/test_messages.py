"""Message layout: the win rate leads, and nothing relies on colour alone."""

from btc15_signal import messages


def test_scoreboard_leads_with_the_win_rate_and_the_sample_count():
    head = messages.scoreboard(15, 12, 0.084, basis="1 contract")
    assert head.startswith("\U0001f3af")
    assert "80% win rate" in head
    assert "12W-3L" in head
    assert "15 signals" in head  # a percentage alone hides how thin the sample is


def test_scoreboard_handles_an_empty_record():
    assert "No settled signals" in messages.scoreboard(0, 0, 0.0)


def test_scoreboard_shows_a_negative_balance_without_a_plus_sign():
    assert "-12.50" in messages.scoreboard(10, 4, -12.50)


def test_bar_tracks_the_fraction_and_never_overflows():
    assert messages.bar(0.0) == "▱" * 10
    assert messages.bar(1.0) == "▰" * 10
    assert messages.bar(0.5) == "▰" * 5 + "▱" * 5
    assert len(messages.bar(2.0)) == 10
    assert len(messages.bar(-1.0)) == 10


def entry(**overrides):
    args = dict(
        head="HEAD",
        live=False,
        side="UP",
        ask=0.91,
        price=81_494.65,
        target=81_158.49,
        remaining=432,
        ticker="KXBTC15M-TEST",
        model=0.963,
        observed=0.80,
        samples=5,
    )
    return messages.entry_alert(**{**args, **overrides})


def test_live_and_manual_entries_use_different_chips_and_titles():
    assert "\U0001f7e2" in entry(live=True) and "ENTRY READY" in entry(live=True)
    assert "\U0001f535" in entry(live=False) and "MANUAL ENTRY" in entry(live=False)


def test_entry_shows_side_direction_price_and_countdown():
    text = entry()
    assert "\U0001f4c8" in text  # up arrow
    assert "<b>BTC UP</b> at <b>91%</b>" in text
    assert "7m 12s" in text
    assert "+41 bps" in text  # distance from the strike


def test_down_entry_flips_the_arrow():
    assert "\U0001f4c9" in entry(side="DOWN", price=80_900.0)


def test_a_note_is_rendered_as_a_warning():
    assert "⚠️" in entry(note="Not auto-validated.")
    assert "⚠️" not in entry(note="")


def test_no_entry_states_the_reason():
    text = messages.no_entry_alert(
        head="HEAD", side="UP", ask=0.91, price=81_494.65, target=81_158.49,
        remaining=300, reason="contract price band", model=0.9, observed=0.5, samples=4,
    )
    assert "NO ENTRY" in text and "contract price band" in text


def test_settlement_states_the_outcome_in_words_not_only_colour():
    win = messages.settlement(
        head="HEAD", ticker="T", side="UP", winner="UP", won=True, target=81_158.49,
        contract_price=0.90, pnl=1.01, qualified=True, basis="1 contract",
    )
    assert "WIN" in win and "\U0001f4b0" in win
    loss = messages.settlement(
        head="HEAD", ticker="T", side="UP", winner="DOWN", won=False, target=81_158.49,
        contract_price=0.90, pnl=-10.06, qualified=False, basis="1 contract",
    )
    assert "LOSS" in loss
    loss_paper = messages.settlement(
        head="HEAD", ticker="T", side="UP", winner="DOWN", won=False, target=81_158.49,
        contract_price=0.90, pnl=None, qualified=False, paper=True,
    )
    assert "rule declined it" in loss_paper
    # A signal nobody traded must carry no money figure, and no green tick that
    # could be mistaken for a payday.
    assert "Loss: $0.00" in loss_paper
    assert "SIGNAL LOST · NOT TRADED" in loss_paper


def test_settlement_omits_pnl_when_no_price_was_recorded():
    text = messages.settlement(
        head="HEAD", ticker="T", side="UP", winner="UP", won=True, target=1.0,
        contract_price=None, pnl=None, qualified=False, basis="1 contract",
    )
    assert "after fees" not in text
    assert "Bought <b>UP</b>" in text


def test_buttons_carry_icons_and_the_proposal_id():
    buttons = messages.execute_buttons(3, "abc123")
    assert buttons[0][0] == "✅ Execute 3"
    assert buttons[0][1] == "execute:abc123"
    assert buttons[1][0].startswith("⏭")
    assert buttons[1][1] == "skip:abc123"


def test_untrusted_text_is_escaped_so_markup_cannot_break_the_message():
    text = messages.no_entry_alert(
        head="HEAD", side="UP", ask=0.5, price=1.0, target=1.0, remaining=60,
        reason="<b>not real markup</b>", model=0.5, observed=0.5, samples=1,
    )
    assert "&lt;b&gt;not real markup&lt;/b&gt;" in text


def test_every_alert_opens_with_the_scoreboard():
    head = messages.scoreboard(10, 8, 0.2)
    assert entry(head=head).startswith(head)
    assert messages.exit_alert(
        head=head, ticker="T", side="UP", price=1.0, target=2.0, remaining=90, bid=0.4
    ).startswith(head)
    assert messages.status(
        head=head, execution_ready=False, manual=True, window=(630, 330)
    ).startswith(head)


def test_status_reports_the_scan_window_and_manual_mode():
    text = messages.status(head="HEAD", execution_ready=False, manual=True, window=(630, 330))
    assert "Manual Execute button: on" in text
    assert "10m → 5m" in text
    assert "NOT CONFIGURED" in text


def test_a_rejected_setup_still_gets_a_button_but_is_marked_as_an_override():
    """Manual mode is pointless if you can only take what automation would."""
    text = entry(live=False, rule_ok=False, rule_reason="contract price band")
    assert "RULE SAYS NO" in text
    assert "contract price band" in text
    assert "your press is the decision" in text
    label = messages.execute_buttons(1, "x", override=True)[0][0]
    assert label == "⚠️ Execute anyway 1"
    assert messages.execute_buttons(1, "x", override=False)[0][0] == "✅ Execute 1"


def test_a_qualifying_setup_says_so():
    assert "Rule: qualifies" in entry(live=False, rule_ok=True)


def test_an_unconfigured_press_names_only_what_is_actually_missing():
    text = entry(missing="TELEGRAM_AUTHORIZED_USER_ID (send /id)")
    assert "still needed: TELEGRAM_AUTHORIZED_USER_ID" in text
    assert "EXECUTION_ENABLED" not in text  # already set; do not send them chasing it
    assert "still needed" not in entry(missing="")


def test_scoreboard_money_is_already_net_and_says_so():
    """Header and settlement line must agree; both are net of fees."""
    head = messages.scoreboard(1, 1, 1.78)
    assert "1.78" in head
    assert "1.90" not in head
