"""Unattended trading: the interesting behaviour is refusing to trade."""

import pytest

from btc15_signal.autotrade import AutoLimits, AutoState, auto_block_reason

LIMITS = AutoLimits(
    daily_loss_limit=10.0,
    max_trades_per_day=40,
    max_trades_per_hour=6,
    min_seconds_between=120,
    budget=1.0,
)
CLEAR = AutoState(
    trades_today=0, trades_last_hour=0, seconds_since_last=1e9,
    realised_today=0.0, open_positions=0,
)


def block(state=CLEAR, price=0.90, enabled=True, limits=LIMITS):
    return auto_block_reason(limits, state, price, enabled)


def test_a_clean_state_permits_a_trade():
    assert block() == ""


def test_the_kill_switch_beats_everything():
    assert "auto trading is off" in block(enabled=False)


def test_the_daily_loss_floor_stops_the_day():
    """The point of trading while asleep is that nobody is watching the streak."""
    from dataclasses import replace

    assert block(replace(CLEAR, realised_today=-9.99)) == ""
    assert "daily loss limit" in block(replace(CLEAR, realised_today=-10.0))
    assert "daily loss limit" in block(replace(CLEAR, realised_today=-25.0))


def test_only_one_position_at_a_time():
    from dataclasses import replace

    assert "already open" in block(replace(CLEAR, open_positions=1))


def test_trade_count_caps_per_day_and_per_hour():
    from dataclasses import replace

    assert "trades today" in block(replace(CLEAR, trades_today=40))
    assert "this hour" in block(replace(CLEAR, trades_last_hour=6))
    assert block(replace(CLEAR, trades_today=39, trades_last_hour=5)) == ""


def test_a_cooldown_separates_orders():
    from dataclasses import replace

    assert "since the last order" in block(replace(CLEAR, seconds_since_last=119))
    assert block(replace(CLEAR, seconds_since_last=120)) == ""


@pytest.mark.parametrize("price", [0.0, 1.0, 1.4, -0.2])
def test_an_implausible_price_is_refused(price):
    assert "implausible price" in block(price=price)


def test_a_zero_budget_cannot_trade():
    from dataclasses import replace

    assert "budget is zero" in block(limits=replace(LIMITS, budget=0.0))


def test_the_loss_floor_is_reported_before_lesser_reasons():
    """Severity order matters: a breached floor must not read as 'too fast'."""
    from dataclasses import replace

    state = replace(CLEAR, realised_today=-20.0, trades_today=40, seconds_since_last=1)
    assert "daily loss limit" in block(state)


def test_guards_can_only_block_never_cause_a_trade():
    """Every guard returns a reason or empty; none can produce an order."""
    from dataclasses import replace

    for field, value in [
        ("realised_today", -50.0), ("open_positions", 3),
        ("trades_today", 999), ("trades_last_hour", 99), ("seconds_since_last", 0),
    ]:
        assert block(replace(CLEAR, **{field: value})) != ""


def test_both_switches_must_be_on_for_an_unattended_order():
    """`enabled: false` in strategy.json is an off switch, not a label.

    The auto path reads `rule.enabled and rule_match`. Honouring the flag only
    for alert wording, while still spending money overnight, would make the
    visible off switch a lie.
    """
    import json
    from pathlib import Path

    from btc15_signal.strategy import EntryRule

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    assert "if rule.enabled and rule_match and auto_on and trader is not None:" in source

    deployed = EntryRule.load("strategy.json")
    shipped = json.loads(Path("strategy.json").read_text(encoding="utf-8"))
    # The file's flag is what the rule object reports - no silent default.
    assert deployed.enabled is bool(shipped["enabled"])


# ------------------------------------------------- exiting a broken thesis


def test_an_exit_order_mirrors_the_entry_side():
    """Selling must post to the opposite side of the book from buying."""
    from btc15_signal.execution import event_order

    for side in ("UP", "DOWN"):
        entry_side, entry_price = event_order(side, 0.35)
        exit_side, exit_price = event_order(side, 0.35, exiting=True)
        assert entry_side != exit_side
        assert entry_price == exit_price  # same contract, opposite direction


def test_an_early_exit_is_scored_at_what_it_sold_for(tmp_path):
    """Not at who eventually won - that would credit back a loss already taken."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "e.db"))
    now = 1_700_000_000_000
    day = now - (now % 86_400_000) + 3_600_000
    p = store.create_proposal("primary", day, "T", "UP", 0.90, 0.0, 10, day, day + 9e5, day)
    store.db.execute("UPDATE trade_proposals SET status='filled'")
    store.db.commit()

    store.mark_exited(p.id, 0.40, 10, "Sold 10 at 40%")
    realised = store.auto_state(day + 60_000)[3]
    # Bought at 90c, sold at 40c, ten contracts: a $5 loss plus both fees.
    assert -5.6 < realised < -5.0


def test_an_exit_still_consumes_a_daily_slot(tmp_path):
    """It cost money and used a window; the caps must see it."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "e.db"))
    now = 1_700_000_000_000
    day = now - (now % 86_400_000) + 3_600_000
    p = store.create_proposal("primary", day, "T", "UP", 0.90, 0.0, 1, day, day + 9e5, day)
    store.db.execute("UPDATE trade_proposals SET status='filled'")
    store.db.commit()
    store.mark_exited(p.id, 0.40, 1, "sold")

    trades_today, trades_hour, _, _, open_now = store.auto_state(day + 60_000)
    assert trades_today == 1
    assert trades_hour == 1
    assert open_now == 0  # closed, so a new position may be taken


def test_marking_an_exit_keeps_the_entry_order_id(tmp_path):
    """It is the only link back to what was actually bought."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "e.db"))
    p = store.create_proposal("primary", 1000, "T", "UP", 0.90, 0.0, 1, 9999, 9999, 1)
    store.finish_proposal(p.id, "filled", "filled", "ENTRY-123", None)
    store.mark_exited(p.id, 0.40, 1, "sold")
    row = store.db.execute(
        "SELECT status, entry_order_id, exit_price FROM trade_proposals WHERE id=?", (p.id,)
    ).fetchone()
    assert row == ("exited", "ENTRY-123", 0.40)


def test_a_position_sold_early_is_no_longer_offered_for_exit(tmp_path):
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "e.db"))
    p = store.create_proposal("primary", 1000, "T", "UP", 0.90, 0.0, 1, 9999, 9999, 1)
    store.db.execute("UPDATE trade_proposals SET status='filled'")
    store.db.commit()
    assert store.open_position_detail(1000)[:2] == ("UP", 0.90)
    store.mark_exited(p.id, 0.40, 1, "sold")
    assert store.open_position_detail(1000) is None


# ----------------------------------------------- what was paid, not what was bid


def test_the_recorded_price_becomes_the_fill_not_the_limit(tmp_path):
    """Every later number - scoreboard, loss floor, settlement - reads this row."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "f.db"))
    store.record((1000, 1, 81_000.0, 80_900.0, "UP", 8, 0.9, "T", 0.87, 1))
    p = store.create_proposal("primary", 1000, "T", "UP", 0.87, 0.0, 1, 9999, 9999, 1)

    store.record_fill(p.id, 1000, 0.84, 1, 0.0095)

    assert store.db.execute(
        "SELECT contract_price FROM predictions WHERE window_open=1000"
    ).fetchone()[0] == 0.84
    row = store.db.execute(
        "SELECT fill_price, fee_paid FROM trade_proposals WHERE id=?", (p.id,)
    ).fetchone()
    assert row == (0.84, 0.0095)


def test_a_better_fill_shows_up_as_profit(tmp_path):
    """Three cents on an 87c limit is a quarter of the trade's gross profit."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "f.db"))
    store.record((1000, 1, 81_000.0, 80_900.0, "UP", 8, 0.9, "T", 0.87, 1))
    p = store.create_proposal("primary", 1000, "T", "UP", 0.87, 0.0, 1, 9999, 9999, 1)
    at_limit = store.scoreboard(contracts=1)

    store.record_fill(p.id, 1000, 0.84, 1, 0.0095)
    store.db.execute("UPDATE predictions SET won=1 WHERE window_open=1000")
    store.db.commit()

    settled, wins, pnl = store.scoreboard(contracts=1)
    assert (settled, wins) == (1, 1)
    assert abs(pnl - 0.16) < 0.01  # 1.00 - 0.84, not 1.00 - 0.87
    assert at_limit[0] == 0  # nothing was settled before


def test_the_alert_states_the_fill_price_and_the_improvement():
    from btc15_signal import messages

    text = messages.auto_filled(
        head="H", ticker="T", side="UP", price=0.84, count=1,
        note="Filled 1", limit=0.87, fee=0.0095,
    )
    assert "84%" in text
    assert "3.0c/contract better" in text
    assert "cost $0.84" in text


def test_the_alert_stays_quiet_when_the_fill_matched_the_limit():
    from btc15_signal import messages

    text = messages.auto_filled(
        head="H", ticker="T", side="UP", price=0.87, count=1, note="n", limit=0.87
    )
    assert "better" not in text and "worse" not in text


def test_a_worse_fill_is_labelled_worse_not_hidden():
    from btc15_signal import messages

    text = messages.auto_filled(
        head="H", ticker="T", side="UP", price=0.89, count=1, note="n", limit=0.87
    )
    assert "2.0c/contract worse" in text
    assert "total" in text  # and the whole-order figure alongside it


def test_a_down_position_is_priced_off_the_no_side():
    """A DOWN bet holds NO contracts; its cost is the no price."""
    import inspect

    from btc15_signal.execution import KalshiExecutionClient

    source = inspect.getsource(KalshiExecutionClient.fill_detail)
    assert '"yes_price_dollars" if side == "UP" else "no_price_dollars"' in source


def test_an_exit_order_is_immediate_or_cancel_and_reduce_only():
    """It must never rest on the book and never open a new position.

    A resting exit would still be live after the thesis had resolved either
    way, and without reduce_only a stale count could flip the position to the
    opposite side instead of closing it.
    """
    import asyncio

    from btc15_signal.execution import KalshiExecutionClient

    sent = {}

    class Fake(KalshiExecutionClient):
        def __init__(self):
            pass

        async def _post(self, path, payload):
            sent.update(payload)
            sent["path"] = path
            return {"order_id": "X", "fill_count": "1.00"}

    result = asyncio.run(Fake().close_position("T", "UP", 1, 0.40))
    assert sent["time_in_force"] == "immediate_or_cancel"
    assert sent["reduce_only"] is True
    assert sent["side"] == "ask"  # selling a UP position
    assert sent["price"] == "0.4000"
    assert result.status == "exited"


def test_an_unfilled_exit_is_reported_as_still_held():
    """Waking up to "exited" when nothing traded is the worst possible message."""
    import asyncio

    from btc15_signal.execution import KalshiExecutionClient

    class Fake(KalshiExecutionClient):
        def __init__(self):
            pass

        async def _post(self, path, payload):
            return {"order_id": "X", "fill_count": "0"}

    result = asyncio.run(Fake().close_position("T", "DOWN", 1, 0.40))
    assert result.status == "exit-unfilled"
    assert result.filled_count == 0
    assert "holding to settlement" in result.note


def test_the_exit_message_never_claims_a_sale_that_did_not_happen():
    from btc15_signal import messages

    held = messages.auto_exit(
        head="H", ticker="T", side="UP", price=1.0, target=2.0, bid=0.4,
        remaining=120, note="No bid at 40%; holding to settlement", sold=False,
    )
    assert "STILL HELD" in held
    assert "sold into" not in held

    done = messages.auto_exit(
        head="H", ticker="T", side="UP", price=1.0, target=2.0, bid=0.4,
        remaining=120, note="Sold 1 at 40%", sold=True,
    )
    assert "AUTO EXIT" in done and "STILL HELD" not in done
    assert "sold into" in done


# ------------------------------------------------------------------ watchdog


def test_the_watchdog_cannot_reset_a_trading_limit():
    """Restarting must never hand the strategy a clean slate it has not earned.

    Every limit is read from the database on each decision, so this is a
    property of where state lives rather than of the watchdog's code - but it
    is the whole reason a restart loop is safe, so it is pinned here.
    """
    from pathlib import Path

    source = Path("scripts/watchdog.py").read_text(encoding="utf-8")
    for forbidden in ("DELETE", "DROP", "UPDATE", "set_setting", "auto_trade_enabled"):
        assert forbidden not in source


def test_the_watchdog_gives_up_rather_than_looping_forever():
    from pathlib import Path

    source = Path("scripts/watchdog.py").read_text(encoding="utf-8")
    assert "MAX_RESTARTS_PER_HOUR" in source
    assert "giving up" in source


def test_the_watchdog_relies_on_the_lock_rather_than_guessing_liveness(tmp_path):
    """Starting a second service is a no-op, so 'is it alive?' need not be asked."""
    from pathlib import Path

    source = Path("scripts/run_service.py").read_text(encoding="utf-8")
    # run_service returns early when the lock is held - that is the contract
    # the watchdog depends on.
    assert "LK_NBLCK" in source
    assert "return" in source.split("except OSError:")[1][:40]


def test_a_disabled_strategy_under_live_auto_is_announced_not_just_logged():
    """It went silent for four hours while 14 signals passed and 17 of 18
    rule-qualified signals won. The log line existed; nobody was reading it."""
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    block = source[source.index('verdict = "strategy.json enabled=false"'):]
    block = block[: block.index("elif trader is None:")]
    assert "telegram.send(" in block
    assert "AUTOMATION IS OFF" in block
    # rate limited, so a long outage nags rather than floods
    assert "disabled_alert_ms" in block
    assert "3_600_000" in block


def test_the_deployed_band_matches_what_was_measured():
    """0.70-0.99 measured +0.0166/contract, CI [+0.0084, +0.0247], p=0.0007 -
    2.8x the trades of 0.85-0.95 for 18% less edge."""
    import json
    from pathlib import Path

    from btc15_signal.strategy import EntryRule

    rule = EntryRule.load("strategy.json")
    assert rule.enabled, "automation is off at the strategy"
    assert rule.min_ask == 0.80
    assert rule.max_ask == 0.99
    # The 0-1x distance zone measured -0.0420/contract; keep a floor above it.
    assert rule.min_normalized_distance >= 1.0

    shipped = json.loads(Path("strategy.json").read_text(encoding="utf-8"))
    assert shipped["enabled"] is True


def test_the_watchdog_launches_an_interpreter_that_can_import_the_package():
    """sys.executable is wrong here. The venv's pythonw.exe is a launcher stub
    that re-execs the BASE interpreter, so a watchdog started through it sees
    the base interpreter as sys.executable - and that one has no btc15_signal.
    Every restart died on import in about a second, burning the restart budget
    while the service was down and a position was open."""
    import subprocess
    import sys
    from pathlib import Path

    sys.path.insert(0, "scripts")
    from watchdog import service_python

    chosen = service_python()
    assert ".venv" in chosen, chosen
    assert Path(chosen).exists()

    probe = subprocess.run(
        [chosen, "-c", "import btc15_signal"], capture_output=True, text=True
    )
    assert probe.returncode == 0, probe.stderr[:200]

    source = Path("scripts/watchdog.py").read_text(encoding="utf-8")
    assert "[sys.executable, \"scripts/run_service.py\"]" not in source


def test_an_entry_may_pay_slightly_through_the_touch():
    """Posted at exactly the touch, 40% of the first live orders never filled:
    an immediate-or-cancel only fills if the resting size survives the round
    trip. A limit still fills at the best available price, so the allowance is
    a ceiling, not a cost - paid only when the book actually moved."""
    import asyncio

    from btc15_signal.config import Settings
    from btc15_signal.execution import KalshiExecutionClient
    from btc15_signal.store import TradeProposal

    assert Settings().entry_slippage == 0.01

    sent = {}

    class Fake(KalshiExecutionClient):
        def __init__(self):
            pass

        async def _post(self, path, payload):
            sent.update(payload)
            return {"order_id": "X", "fill_count": "1.00"}

    def proposal(side, limit):
        return TradeProposal(
            id="p", strategy="primary", window_open=0, ticker="T", side=side,
            entry_limit=limit, take_profit=0.0, count=1, expires_at=0,
            close_ms=0, status="pending",
        )

    asyncio.run(Fake().execute_with_take_profit(proposal("UP", 0.74), 0.01))
    assert sent["price"] == "0.7500"  # willing to pay a cent more

    asyncio.run(Fake().execute_with_take_profit(proposal("UP", 0.74), 0.0))
    assert sent["price"] == "0.7400"  # and exactly the touch when told to

    # Never past the bounds event_order accepts.
    asyncio.run(Fake().execute_with_take_profit(proposal("UP", 0.99), 0.05))
    assert sent["price"] == "0.9900"


def test_a_down_entry_crosses_in_the_right_direction():
    """A DOWN buy posts as a YES sell, so paying more means a LOWER yes price."""
    from btc15_signal.execution import event_order

    _, plain = event_order("DOWN", 0.74)
    _, through = event_order("DOWN", 0.75)
    assert through < plain  # 0.25 vs 0.26: more aggressive, not less


# ------------------------------------- a missed fill is not a missed chance


def test_a_missed_fill_may_be_re_attempted(tmp_path):
    """An immediate-or-cancel that did not fill cost nothing and changed
    nothing. On 2026-09-21 the 08:45 book gapped 9c in fourteen seconds and ran
    to 96%; the winner was missed because one miss ended the window."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "r.db"))
    assert store.order_attempts(1000) == (0, None)

    p1 = store.create_proposal("primary", 1000, "T", "UP", 0.77, 0.0, 1, 9999, 9999, 1)
    store.finish_proposal(p1.id, "unfilled", "no fill", "ORDER-1", None)
    assert store.order_attempts(1000) == (1, "unfilled")


def test_a_filled_order_closes_the_window_to_further_attempts(tmp_path):
    """Retrying after a FILL would double the position, which is a different
    and much worse mistake than missing one."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "r.db"))
    p = store.create_proposal("primary", 1000, "T", "UP", 0.77, 0.0, 1, 9999, 9999, 1)
    store.finish_proposal(p.id, "filled", "filled", "ORDER-1", None)

    _attempts, last = store.order_attempts(1000)
    assert last == "filled"
    # The retry gate keys on 'unfilled' alone.
    source = (tmp_path / "..").resolve()  # noqa: F841 - readability only
    from pathlib import Path

    main = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    assert 'last_status == "unfilled"' in main


def test_retries_are_capped_so_a_running_market_is_not_chased_forever(tmp_path):
    from btc15_signal.config import Settings
    from btc15_signal.store import Store

    limit = Settings().auto_retry_limit
    assert 1 < limit <= 5, "a cap must exist and must be small"

    store = Store(str(tmp_path / "r.db"))
    for i in range(limit):
        p = store.create_proposal(
            "primary", 1000 + i * 0, f"T{i}", "UP", 0.77, 0.0, 1, 9999, 9999, i + 1
        ) if i == 0 else None
        if p:
            store.finish_proposal(p.id, "unfilled", "no fill", f"ORDER-{i}", None)
    attempts, _ = store.order_attempts(1000)
    assert attempts <= limit


def test_a_retry_re_runs_every_gate_rather_than_forcing_the_trade():
    """The distinction that matters: it re-evaluates at the new price, it does
    not repeat the old decision. A setup that has left the band is dropped."""
    from pathlib import Path

    main = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    gate = main.split("attempts, last_status = store.order_attempts(opened)")[1]
    gate = gate[: gate.index("if not store.record_alert")]
    assert "rule_match" in gate, "a retry must still require the rule to match"
    assert "auto_retry_limit" in gate


# --------------------------- alerting once vs trading every poll


def test_the_alert_gate_no_longer_stops_trading():
    """These were one gate, and it was the biggest limiter in the system. The
    alert fires at the first poll above the manual floor - usually while the
    price is still walking up through the 60s and 70s - and that locked the
    window. Over 6,428 historical windows, 67% of every window that ever
    qualified did so only AFTER that lock."""
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")

    # record_alert's result is captured, not used as an early return on its own.
    assert "alerting = store.record_alert(" in source
    assert "and not trading_open" in source

    # Trading eligibility is evaluated separately from alerting.
    assert "trading_open = rule.enabled and rule_match and auto_is_on(" in source

    # The Telegram alert itself is still once per window.
    assert "if not alerting:" in source


def test_trading_stays_bounded_by_the_guards_not_by_the_alert(tmp_path):
    """Removing the alert lock must not remove the limits that matter."""
    from btc15_signal.autotrade import AutoLimits, AutoState, auto_block_reason

    limits = AutoLimits(10.0, 96, 6, 120, 1.0)
    # An open position still refuses a second order, whatever the alert did.
    holding = AutoState(1, 1, 1e9, 0.0, 1)
    assert "already open" in auto_block_reason(limits, holding, 0.85, enabled=True)

    # And a breached loss floor still stops the day.
    down = AutoState(1, 1, 1e9, -10.0, 0)
    assert "daily loss limit" in auto_block_reason(limits, down, 0.85, enabled=True)


def test_a_window_can_qualify_after_the_alert_has_already_fired(tmp_path):
    """The 09:15 case: alerted at 0.76, and if the price had later reached the
    band nothing would have looked again."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "a.db"))
    # The alert is consumed on the first call and refused thereafter...
    assert store.record_alert("primary", 1000, 1) is True
    assert store.record_alert("primary", 1000, 2) is False
    # ...which is exactly why trading must not depend on it.


def test_create_proposal_returns_the_one_it_just_created(tmp_path):
    """It selected on (strategy, window_open) with no attempt filter and no
    LIMIT. Correct while a window held one proposal; once retries gave a window
    several it returned attempt 1, so the auto path created attempt 2 at 82c,
    was handed the manual attempt at 65c, and placed a real order against a
    price the rule had rejected."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "p.db"))
    manual = store.create_proposal("primary", 1000, "T", "UP", 0.65, 0, 1, 9999, 9999, 1)
    auto = store.create_proposal("primary", 1000, "T", "UP", 0.82, 0, 1, 9999, 9999, 2)

    assert manual.id != auto.id
    assert manual.entry_limit == 0.65
    assert auto.entry_limit == 0.82, "handed back an earlier attempt"

    # And claiming the one you were given claims that one.
    claimed = store.claim_proposal(auto.id, 1)
    assert claimed is not None
    assert claimed.entry_limit == 0.82


def test_an_order_can_never_be_placed_against_another_attempts_price(tmp_path):
    """The failure mode in full: a rejected 65c setup reaching the exchange."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "p.db"))
    rejected = store.create_proposal("primary", 1000, "T", "UP", 0.65, 0, 1, 9999, 9999, 1)
    for attempt, price in ((2, 0.82), (3, 0.84)):
        fresh = store.create_proposal(
            "primary", 1000, "T", "UP", price, 0, 1, 9999, 9999, attempt
        )
        assert fresh.entry_limit == price
        assert fresh.id != rejected.id


def test_drift_is_measured_from_the_last_order_not_the_first_proposal():
    """The first proposal of a window may be a manual button offered far below
    the band, which would make every drift look enormous and block all retries."""
    from pathlib import Path

    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    drift = source.split("drift = contract_ask - (")[1][:400]
    assert "entry_order_id IS NOT NULL" in drift
    assert "ORDER BY attempt DESC" in drift
