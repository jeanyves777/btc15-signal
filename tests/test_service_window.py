"""The service must look at the whole entry window, not one minute."""

from btc15_signal.config import Settings
from btc15_signal.store import Store


def in_window(settings: Settings, remaining: int) -> bool:
    """The gate primary_signal applies before doing any work."""
    return settings.entry_to_seconds <= remaining <= settings.entry_from_seconds


def last_look(settings: Settings, remaining: int) -> bool:
    return remaining - settings.poll_seconds < settings.entry_to_seconds


def test_entry_window_covers_ten_minutes_down_to_five_and_a_half():
    settings = Settings()
    assert in_window(settings, 10 * 60)
    assert in_window(settings, 9 * 60)
    assert in_window(settings, 7 * 60)
    assert in_window(settings, 6 * 60)


def test_window_excludes_the_minutes_that_measured_negative():
    """At three minutes left the same trade loses money, so never look there."""
    settings = Settings()
    assert not in_window(settings, 3 * 60)
    assert not in_window(settings, 60)
    assert not in_window(settings, 12 * 60)


def test_the_old_single_minute_trigger_would_have_missed_most_of_the_window():
    settings = Settings()
    old_only = {
        seconds
        for seconds in range(60, 12 * 60, 10)
        if abs(seconds - 5 * 60) <= settings.entry_tolerance_seconds
    }
    now = {seconds for seconds in range(60, 12 * 60, 10) if in_window(settings, seconds)}
    assert len(now) > 5 * len(old_only)  # 31 poll slots vs 5
    # The old trigger sat entirely BELOW the new window, in the minutes that
    # measured flat to negative.
    assert old_only.isdisjoint(now)
    assert max(old_only) < min(now)


def test_last_look_fires_once_at_the_bottom_of_the_window():
    settings = Settings()
    assert not last_look(settings, 10 * 60)
    assert last_look(settings, settings.entry_to_seconds)


def test_the_reversal_exit_is_off_because_it_lost_money():
    """Measured on 5,753 paired historical trades, same entries, exit vs hold:
    -$0.0162/contract, 95% CI [-0.0233, -0.0089]. It fired on 50% of trades and
    was worse than holding in 79% of them, against a gross entry edge of about
    +$0.017 - it was destroying the whole edge.

    Every variant lost too: persistence of 2-3 minutes, margins of 5-40bps, and
    only cutting while the bid was still 0.55 or 0.70. The reason is arithmetic
    rather than tuning: the contract price IS the market's probability, so by
    optional stopping any exit is EV-neutral before costs and pays the spread
    a second time plus a second fee."""
    settings = Settings()
    assert not settings.exit_on_reversal
    assert settings.exit_min_seconds >= 60  # still guards a manual exit


def test_open_position_only_returns_a_filled_primary_trade(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    assert store.open_position(1000) is None
    store.create_proposal("primary", 1000, "T", "UP", 0.90, 0.0, 1, 9999, 9999, 1)
    assert store.open_position(1000) is None  # still pending, nothing to exit
    store.db.execute("UPDATE trade_proposals SET status='filled' WHERE window_open=1000")
    store.db.commit()
    assert store.open_position(1000) == ("UP", 0.90)


def test_deployed_rule_targets_the_measured_band():
    from btc15_signal.strategy import EntryRule

    rule = EntryRule.load("strategy.json")
    # 0.80-0.99. Widened to 0.70 on GROSS edge, then corrected: the Kalshi fee
    # is 0.07*P*(1-P), largest mid-book, so the cheap end of the band is the
    # expensive end after fees. Net of fees, 0.70-0.99 measures +0.0072 with a
    # 95% CI of [-0.0009, +0.0154] - it straddles zero. 0.80-0.99 is +0.0094
    # [+0.0009, +0.0177], keeping 40% more trades than 0.85 with a lower bound
    # still above zero.
    assert rule.min_ask == 0.80
    assert rule.max_ask == 0.99
    # The rule is the master switch for unattended trading. Turning it off here
    # stops automation outright, independently of the /auto flag - which is
    # exactly what silently happened for four hours on 2026-09-21.
    assert rule.enabled
    # The 0-1x distance zone measured -0.0420/contract, so a floor stays.
    assert rule.min_normalized_distance >= 1.0


def offers_button(ask: float, qualified: bool = False) -> bool:
    """The gate primary_signal applies when deciding to attach an Execute button."""
    settings = Settings()
    return qualified or (settings.manual_execution_enabled and ask >= settings.manual_min_ask)


def test_a_qualifying_setup_always_gets_a_button():
    assert offers_button(0.90, qualified=True)
    assert offers_button(0.64, qualified=True)  # rule said yes; respect it


def test_a_near_miss_below_the_rule_still_gets_an_override_button():
    """0.84 is one cent under the 0.85 floor - a judgement call, not noise."""
    assert offers_button(0.84)
    assert offers_button(0.80)


def test_a_setup_with_no_measured_edge_offers_nothing_to_press():
    """0.64 sits in the band that measured flat to negative over 68 days."""
    assert not offers_button(0.64)
    assert not offers_button(0.50)
    assert not offers_button(0.30)


def test_the_override_floor_sits_below_the_rule_band_but_well_above_noise():
    from btc15_signal.strategy import EntryRule

    settings = Settings()
    rule = EntryRule.load("strategy.json")
    assert settings.manual_min_ask < rule.min_ask  # leaves room for near-misses
    # Not all the way down to a coin flip: below about 0.60 there is no measured
    # favourite-longshot edge left to be a near-miss of.
    assert settings.manual_min_ask >= 0.60


def test_a_button_lives_long_enough_for_a_human_to_press_it():
    """20s suits an automated press; a person needs to pick up a phone.

    Safe to lengthen because the press re-validates ticker and live ask before
    submitting, so a stale proposal is rejected rather than filled badly.
    """
    settings = Settings()
    assert settings.proposal_seconds >= 120
    # but never outlive the entry window it belongs to
    assert settings.proposal_seconds <= settings.entry_from_seconds - settings.entry_to_seconds


def alerts_now(ask: float, remaining: int, rule_enabled: bool = False) -> bool:
    """Reproduces primary_signal's decision to send an alert this poll."""
    settings = Settings()
    qualified = rule_enabled  # stands in for rule.enabled and rule_match
    offer = qualified or (settings.manual_execution_enabled and ask >= settings.manual_min_ask)
    last_look = remaining - settings.poll_seconds < settings.entry_to_seconds
    return offer or last_look


def test_an_in_band_setup_alerts_at_the_top_of_the_window_not_the_bottom():
    """The validated strategy enters at the FIRST in-band minute.

    Gating alerts on `qualified` made this impossible while the rule is
    disabled, so every alert arrived at the last look around 5m30s - a worse
    price, and outside the 6-11 minute range the edge was measured in.
    """
    assert alerts_now(0.98, remaining=10 * 60)
    assert alerts_now(0.88, remaining=9 * 60)
    assert alerts_now(0.85, remaining=8 * 60)


def test_an_out_of_band_setup_waits_for_the_last_look():
    assert not alerts_now(0.64, remaining=10 * 60)
    assert not alerts_now(0.64, remaining=7 * 60)
    assert alerts_now(0.64, remaining=Settings().entry_to_seconds)  # one summary


def test_a_disabled_rule_no_longer_suppresses_early_alerts():
    """enabled=false governs automation only; it must not delay the alert."""
    assert alerts_now(0.95, remaining=10 * 60, rule_enabled=False)
    assert alerts_now(0.95, remaining=10 * 60, rule_enabled=True)


def test_a_rejected_press_says_which_check_failed_and_by_how_much():
    from btc15_signal.main import rejection_reason

    rolled = rejection_reason("KXBTC15M-B", 0.83, "KXBTC15M-A", 0.83, "UP")
    assert "rolled to KXBTC15M-B" in rolled

    moved = rejection_reason("KXBTC15M-A", 0.85, "KXBTC15M-A", 0.83, "UP")
    assert "moved to 85%" in moved and "quoted 83%" in moved and "+2c" in moved

    # a better price than quoted is not a rejection
    assert rejection_reason("KXBTC15M-A", 0.81, "KXBTC15M-A", 0.83, "UP") == ""
    assert rejection_reason("KXBTC15M-A", 0.83, "KXBTC15M-A", 0.83, "UP") == ""


def size_store(tmp_path):
    from btc15_signal.store import Store

    return Store(str(tmp_path / "size.db"))


def test_default_order_size_is_one_dollar(tmp_path):
    """Live money: the default must be the smallest thing that can trade."""
    from btc15_signal.main import budget_for

    settings = Settings()
    assert settings.manual_budget == 1.0
    assert settings.auto_budget == 1.0
    store = size_store(tmp_path)
    assert budget_for(store, settings, auto=False) == 1.0
    assert budget_for(store, settings, auto=True) == 1.0


def test_size_command_sets_manual_and_auto_independently(tmp_path):
    from btc15_signal.main import budget_for, handle_size_command

    settings, store = Settings(), size_store(tmp_path)
    handle_size_command(store, settings, "/size 5", 0)
    handle_size_command(store, settings, "/autosize 2", 0)
    assert budget_for(store, settings, auto=False) == 5.0
    assert budget_for(store, settings, auto=True) == 2.0


def test_a_runtime_size_survives_without_a_restart(tmp_path):
    """Stored in the database, not .env - a restart mid-window killed it once."""
    from btc15_signal.main import budget_for, handle_size_command
    from btc15_signal.store import Store

    settings = Settings()
    path = str(tmp_path / "persist.db")
    handle_size_command(Store(path), settings, "/size 7.5", 0)
    assert budget_for(Store(path), settings, auto=False) == 7.5


def test_size_rejects_nonsense_and_fat_fingers(tmp_path):
    from btc15_signal.main import budget_for, handle_size_command

    settings, store = Settings(), size_store(tmp_path)
    assert "Could not read" in handle_size_command(store, settings, "/size abc", 0)
    assert "must be between" in handle_size_command(store, settings, "/size 100000", 0)
    assert "must be between" in handle_size_command(store, settings, "/size -3", 0)
    assert budget_for(store, settings, auto=False) == 1.0  # unchanged by bad input


def test_a_budget_converts_to_whole_contracts_rounding_down(tmp_path):
    from btc15_signal.validation import contracts_for_budget

    assert contracts_for_budget(1.0, 0.86) == 1      # $0.86 at risk
    assert contracts_for_budget(5.0, 0.86) == 5      # $4.30 at risk, never over budget
    assert contracts_for_budget(0.10, 0.86) == 1     # floor of one; zero cannot trade


# ------------------------------------------------------- the auto kill switch


def test_auto_is_off_until_switched_on_even_when_env_permits(tmp_path):
    """The .env flag may permit; only a deliberate /auto on starts trading."""
    from btc15_signal.main import auto_is_on

    store = Store(str(tmp_path / "a.db"))
    assert not auto_is_on(store, Settings())
    permissive = Settings(auto_trade_enabled=True)
    assert auto_is_on(store, permissive)  # env default applies...
    store.set_setting("auto_trade_enabled", 0.0, 1_000)
    assert not auto_is_on(store, permissive)  # ...but /auto off overrides it


def test_turning_auto_on_is_refused_without_execution_configured(tmp_path):
    """An 'on' that silently does nothing is worse than an error at 2am."""
    from btc15_signal.main import auto_is_on, handle_auto_command

    store = Store(str(tmp_path / "a.db"))
    reply = handle_auto_command(store, Settings(), "/auto on", 1_000, trader_ready=False)
    assert "Cannot enable" in reply
    assert not auto_is_on(store, Settings())


def test_turning_auto_off_always_works(tmp_path):
    """The stop must never depend on the thing being stopped being healthy."""
    from btc15_signal.main import auto_is_on, handle_auto_command

    store = Store(str(tmp_path / "a.db"))
    handle_auto_command(store, Settings(), "/auto on", 1_000, trader_ready=True)
    assert auto_is_on(store, Settings())
    reply = handle_auto_command(store, Settings(), "/auto off", 2_000, trader_ready=False)
    assert "OFF" in reply
    assert not auto_is_on(store, Settings())


def test_the_on_confirmation_states_the_limits_it_will_stop_at(tmp_path):
    from btc15_signal.main import handle_auto_command

    store = Store(str(tmp_path / "a.db"))
    settings = Settings()
    reply = handle_auto_command(store, settings, "/auto on", 1_000, trader_ready=True)
    assert f"{settings.auto_max_trades_per_day} trades" in reply
    assert "/auto off" in reply


def test_bare_auto_reports_status_without_changing_anything(tmp_path):
    from btc15_signal.main import auto_is_on, handle_auto_command

    store = Store(str(tmp_path / "a.db"))
    before = auto_is_on(store, Settings())
    reply = handle_auto_command(store, Settings(), "/auto", 1_000, trader_ready=True)
    assert "AUTO TRADING" in reply
    assert auto_is_on(store, Settings()) is before


def test_a_breached_loss_floor_survives_a_restart(tmp_path):
    """A crash loop must not become a way to reset the daily stop."""
    from btc15_signal.autotrade import AutoState, auto_block_reason
    from btc15_signal.main import auto_limits

    path = str(tmp_path / "a.db")
    store = Store(path)
    store.set_setting("auto_trade_enabled", 1.0, 1_000)
    limits = auto_limits(store, Settings())

    losing = AutoState(1, 1, 1e9, -limits.daily_loss_limit, 0)
    assert "daily loss limit" in auto_block_reason(limits, losing, 0.9, enabled=True)

    reopened = Store(path)  # the restart
    from btc15_signal.main import auto_is_on

    assert auto_is_on(reopened, Settings())  # the switch persisted...
    state = AutoState(*reopened.auto_state(2_000))
    # ...and so did the realised P&L the floor is measured against.
    assert state.realised_today == store.auto_state(2_000)[3]


def test_auto_limits_take_a_telegram_override_over_the_env(tmp_path):
    from btc15_signal.main import auto_limits

    store = Store(str(tmp_path / "a.db"))
    assert auto_limits(store, Settings()).max_trades_per_hour == 6
    store.set_setting("auto_max_trades_per_hour", 2.0, 1_000)
    assert auto_limits(store, Settings()).max_trades_per_hour == 2
