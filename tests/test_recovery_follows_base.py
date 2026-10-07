"""Recovery follows the base (operator, 2026-09-27).

At 00:00 New York on 2026-09-27 the capital review moved every instance from a
base of one contract to two ($79.61 at $30 a contract). Three things had been
written for a base of one, and all three broke without a single error:

  * the loss step was a flat $2. $2 buys 2 in the 0.70-0.79 band, 2 is not
    above a base of 2, so the step returned the base with an EMPTY reason -
    no recovery, no log line, and no mirror recovery either, because the
    mirror only follows an actual upsize;
  * "already spent" was `count > 1`, so the first ordinary base-2 entry after
    a loss marked the step used before it could fire;
  * the add-on stand-down re-ran the step at base 1 instead of reading the
    position - which was also wrong at base 1.

The operator's rule: "Upsizing recovery must follow as well" - automatically
double. The step is $2 PER BASE CONTRACT and the add is +1 PER BASE CONTRACT,
so at base 1 both are exactly what they always were.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import btc15_signal.main as main  # noqa: E402
from btc15_signal import messages  # noqa: E402
from btc15_signal.capital import Capital, ny_day  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.recovery_add import AddLimits, evaluate, max_net_profit  # noqa: E402
from btc15_signal.recovery_add_runner import RecoveryAddRunner  # noqa: E402
from btc15_signal.store import RecoveryState, Store  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged  # noqa: E402


# THE COMBO MECHANICS, tested on the pre-2026-09-28 map. Since then BTC has no
# partner ("BTC AND GOLD ONLY I SAID"; FINDINGS 108) and its entries carry no
# recovery label at all - pinned in tests/test_live_instruments.py. The rules
# for WHEN a recovery is due still run for ETH->SOL and SOL->BTC, on this code.
@pytest.fixture(autouse=True)
def _mechanics_map(monkeypatch):
    from btc15_signal import combo_recovery
    monkeypatch.setattr(combo_recovery, "PARTNERS",
                        {"BTC": ("SOL",), "ETH": ("SOL",), "SOL": ("BTC",)})

DAY = 1_790_193_600_000          # a window inside one New York day
M = 900_000
BAND = (0.70, 0.72, 0.75, 0.77, 0.79)


def tiered(tmp_path, tier, name="s.db"):
    store = Store(str(tmp_path / name))
    store.record_capital_day(Capital(
        ny_day=ny_day(DAY), reconciled_cash=79.61, open_exposure=0.0,
        base_contracts=tier, account_ceiling=30.0, reconciled_ms=DAY))
    return store


def entry(store, pid, window, count, *, won=None, fill=0.80):
    """A primary entry; `won` settles it (True/False) or leaves it open."""
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker,"
        " side, entry_limit, take_profit, count, expires_at, close_ms,"
        " status, created_at, fill_price, fee_paid) "
        "VALUES (?,'primary',?,?,'UP',?,0,?,?,?,'filled',?,?,0.02)",
        (pid, window, f"T-{pid}", fill, count, window + 60_000, window + M,
         window, fill))
    if won is not None:
        store.db.execute(
            "INSERT INTO settlements (ticker, event_ticker, market_result,"
            " yes_count, yes_cost, no_count, no_cost, revenue_cents, fee_cost,"
            " pnl, settled_ms, synced_at, window_ms) "
            "VALUES (?,?,?,0,0,0,0,0,0,?,?,?,?)",
            (f"T-{pid}", f"T-{pid}", "yes" if won else "no",
             0.30 if won else -1.60, window + M, window, window))
    store.db.commit()


STEP = Settings(loss_step_enabled=True, loss_step_budget=2.0)


# ------------------------------------------------------------- the step

@pytest.mark.parametrize("ask", BAND)
def test_at_base_2_the_step_doubles_the_base(tmp_path, ask):
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    count, why = main.loss_step_size(store, STEP, 2, ask)
    assert count == 4, why
    assert "x base 2 = 4" in why


@pytest.mark.parametrize("ask", BAND)
def test_at_base_1_the_step_is_the_two_dollar_step_it_always_was(tmp_path, ask):
    store = tiered(tmp_path, 1)
    entry(store, "loss", DAY, 1, won=False)
    count, why = main.loss_step_size(store, STEP, 1, ask)
    assert count == 2, why


@pytest.mark.parametrize("ask", BAND)
def test_a_flat_two_dollars_would_not_have_been_an_upsize_at_base_2(tmp_path, ask):
    """What ran from 00:00 to the fix, stated as arithmetic: a flat $2 buys
    exactly the base of 2 everywhere in the band, so the old code returned the
    base with an empty reason. The per-base step must come out above it."""
    assert main.contracts_for_budget(2.0, ask) == 2
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    count, why = main.loss_step_size(store, STEP, 2, ask)
    assert count > 2 and why


def test_an_ordinary_base_2_entry_does_not_spend_the_step(tmp_path):
    """Loss, then a base-2 win at 0.88 (out of band), then an in-band setup.
    `count > 1` read that base-2 win as the step having fired."""
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    entry(store, "base-win", DAY + M, 2, won=True, fill=0.88)
    assert store.upsized_since(DAY) is False
    count, why = main.loss_step_size(store, STEP, 2, 0.75)
    assert count == 4, why


def test_the_step_still_fires_once_per_loss(tmp_path):
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    entry(store, "stepped", DAY + M, 4, won=True, fill=0.75)
    assert store.upsized_since(DAY) is True
    assert main.loss_step_size(store, STEP, 2, 0.75) == (
        2, "recovery already taken for this loss")


def test_without_capital_sizing_the_base_is_one_whatever_the_review_said(tmp_path):
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    entry(store, "two", DAY + M, 2, won=True)
    assert store.upsized_since(DAY, tiered=False) is True
    assert store.entry_was_upsized("two", tiered=False) is True
    assert store.entry_was_upsized("two") is False


def test_a_day_with_no_review_is_base_1(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    assert store.base_tier_at(DAY) == 1
    entry(store, "two", DAY, 2)
    assert store.entry_was_upsized("two") is True


def test_an_entry_sized_before_the_days_review_was_sized_at_1(tmp_path):
    """The review is written by the first poll that can read the broker. If
    midnight's could not, the first entries of the day were sized at 1 - and
    a step taken then (2 contracts) must still read as the step once the
    review lands and says 2. Otherwise it fires a second time on one loss."""
    store = Store(str(tmp_path / "s.db"))
    entry(store, "loss", DAY, 1, won=False)
    entry(store, "gap-step", DAY + M, 2, won=True, fill=0.75)
    store.record_capital_day(Capital(
        ny_day=ny_day(DAY), reconciled_cash=79.61, open_exposure=0.0,
        base_contracts=2, account_ceiling=30.0, reconciled_ms=DAY + 2 * M))
    assert store.base_tier_at(DAY + M) == 1
    assert store.base_tier_at(DAY + 2 * M) == 2
    assert store.entry_was_upsized("gap-step") is True
    assert main.loss_step_size(store, STEP, 2, 0.75) == (
        2, "recovery already taken for this loss")


def test_the_step_still_decides_when_a_recovery_is_due_at_base_2(tmp_path):
    """The loss step is the arming authority for the combo: at base 2 its
    answer is still "above base", which `recovery_sizing` turns into a combo
    at base size rather than an upsize."""
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    count, step_reason = main.loss_step_size(store, STEP, 2, 0.75)
    assert count > 2 and step_reason
    assert main.recovery_sizing(store, STEP, 2, "", 0.75)[5] is True


# ------------------------------------------------------- the stand-down

def test_at_base_2_the_add_on_stands_down_only_on_the_stepped_entry(tmp_path):
    from btc15_signal.kalshi import KalshiMarket

    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    entry(store, "base", DAY + M, 2)
    entry(store, "stepped", DAY + 2 * M, 4)
    in_band = KalshiMarket(ticker="T", target=64.0, open_ms=0, close_ms=M,
                           yes_ask=0.75, no_ask=0.25, yes_bid=0.74, no_bid=0.24)
    # The step is armed and the ask is in band - but this entry went out at
    # the base, so the add-on is the only recovery it has. It must run.
    assert main.add_on_stands_down(
        store, STEP, in_band, ("UP", 0.80, 2, "T-base", "base")) is False
    assert main.add_on_stands_down(
        store, STEP, in_band, ("UP", 0.75, 4, "T-stepped", "stepped")) is True


# -------------------------------------------------------------- the add

def _features(side="UP"):
    from btc15_signal.brti import BRTIFeatures
    signed = 50.0 if side == "UP" else -50.0
    return BRTIFeatures(
        event_ticker="E", ts_ms=0, target=86_430.49,
        value=86_430.49 * (1 + signed / 10_000), signed_distance_bps=signed,
        brti_momentum_bps=20.0 if side == "UP" else -20.0,
        brti_volatility_bps=3.0, brti_normalized_distance=15.0,
        samples=300, span_ms=300_000, stale=False,
        settlement_projection=86_400.0,
    )


def _judge(**over):
    args = dict(
        features=_features(), entry_side="UP", entry_fill=0.77,
        current_ask=0.78, crossed_since_entry=False, remaining_s=500,
        required_per_trade=0.0, recovery_active=True, already_added=False,
        open_exposure=0.0, limits=AddLimits(), fee=kalshi_fee_charged,
    )
    args.update(over)
    return evaluate(**args)


def test_the_add_at_1_and_1_is_the_arithmetic_it_always_was():
    d = _judge()
    assert d.place
    potential = max_net_profit(2, 0.76, kalshi_fee_charged)
    assert f"{potential:+.4f}" in d.reason
    assert d.reason.startswith("resting 1 at 0.75")


def test_the_add_at_base_2_rests_2_and_is_judged_on_4_contracts():
    d = _judge(base_count=2, add_count=2)
    assert d.place
    potential = max_net_profit(4, 0.76, kalshi_fee_charged)
    assert f"{potential:+.4f}" in d.reason
    assert d.reason.startswith("resting 2 at 0.75")


def test_the_combined_average_is_weighted_by_the_real_counts():
    d = _judge(base_count=2, add_count=1, required_per_trade=99.0)
    combined = round((2 * 0.77 + 1 * 0.75) / 3, 6)
    assert f"combined average {combined:.4f}" in d.reason


def test_the_cap_counts_every_contract_the_add_would_rest():
    assert _judge(open_exposure=29.0, add_count=1).place is True      # 29.75
    refused = _judge(open_exposure=29.0, add_count=2)                  # 30.50
    assert refused.place is False and "cap" in refused.reason


def test_the_runner_sizes_the_add_per_base_contract(tmp_path):
    store = tiered(tmp_path, 2)
    entry(store, "e", DAY, 2)
    assert RecoveryAddRunner(Settings(), store).add_count("e") == 2
    one = tiered(tmp_path, 1, "t1.db")
    entry(one, "e", DAY, 1)
    assert RecoveryAddRunner(Settings(), one).add_count("e") == 1
    assert RecoveryAddRunner(
        Settings(capital_sizing_enabled=False), store).add_count("e") == 1
    assert RecoveryAddRunner(Settings(), store).add_count("unknown") == 1


def test_at_base_2_the_runner_places_a_2_contract_add(tmp_path):
    """End to end through the runner and a fake broker: a base-2 position
    after a loss gets a 2-contract add, recorded and placed as 2."""
    import test_recovery_add_runner as harness

    settings, store = harness.make(tmp_path)
    store.db.execute("UPDATE trade_proposals SET count = 2 WHERE id = 'p1'")
    # The harness creates its entry at NOW, so the review is NOW's day.
    store.record_capital_day(Capital(
        ny_day=ny_day(harness.NOW), reconciled_cash=79.61,
        open_exposure=0.0, base_contracts=2, account_ceiling=30.0,
        reconciled_ms=harness.NOW - 1))
    store.db.commit()
    trader = harness.FakeTrader()
    harness.run(RecoveryAddRunner(settings, store), trader)
    assert len(trader.placed) == 1
    assert trader.placed[0]["count"] == 2
    assert store.open_add(harness.WINDOW)["count"] == 2


# ------------------------------------------------------------ the message

def test_the_armed_message_states_the_combo_at_todays_base():
    """Since 2026-09-27 the recovery is a combo at base size: the message
    names the base, the partner and its band, and says nothing is upsized."""
    state = RecoveryState(active=True, deficit=1.60, markets=0, opened_ms=0,
                          steps=4, initial=1.60, cycle_id="c1", wins=0)
    text = messages.recovery_armed_message(state, "", snapshot=None,
                                           loss_step=2.0, base=2, combo=True,
                                           partner="SOL")
    assert "recovery combo at base size (2)" in text
    assert "SOL on its own signal side, priced 0.70-0.85" in text
    assert "Nothing is upsized" in text
    assert "2x the base" not in text
    off = messages.recovery_armed_message(state, "", snapshot=None,
                                          loss_step=2.0, base=2, combo=False)
    assert "Recovery combos are off" in off


@pytest.mark.parametrize("base", [5, 8, 12])
def test_a_recovery_is_still_due_at_any_base(tmp_path, base):
    """The base now scales with capital, uncapped (operator, 2026-09-27).
    Capped at 8, the step's count at a base of 8+ equalled the base, read as
    "not due", and every recovery would have ended silently."""
    store = tiered(tmp_path, base)
    entry(store, "loss", DAY, base, won=False)
    count, why, b, _, _, due = main.recovery_sizing(store, STEP, base, "", 0.75)
    assert (count, b, due) == (base, base, True), why


# ------------------------------------ what was DECIDED, recorded on the row
#
# From the review of this change. "Was this entry the step" used to be read
# back off `count`, which `record_fill` overwrites with the FILLED count, and
# off the day's tier, which is not always the base the order path used. Both
# are now recorded when the proposal is created.

def proposal(store, window, count, base, *, status="filled", filled=None):
    p = store.create_proposal("primary", window, f"T-{window}", "UP", 0.75, 0,
                              count, window + 60_000, window + M, window,
                              base_count=base)
    if filled is not None:
        store.record_fill(p.id, window, 0.75, filled, 0.02)
    store.finish_proposal(p.id, status, "")
    return p.id


def test_a_partly_filled_step_is_still_the_step(tmp_path):
    """A 4-lot step that fills 2 at base 2 used to read as a base entry: the
    step fired again on the same loss and the add-on rested behind it."""
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    pid = proposal(store, DAY + M, 4, 2, filled=2)
    assert store.db.execute(
        "SELECT count FROM trade_proposals WHERE id=?", (pid,)).fetchone()[0] == 2
    assert store.entry_was_upsized(pid) is True
    assert store.upsized_since(DAY) is True
    assert main.loss_step_size(store, STEP, 2, 0.75)[1] == \
        "recovery already taken for this loss"
    assert main.add_on_stands_down(
        store, STEP, None, ("UP", 0.75, 2, "T", pid)) is True


def test_a_step_that_bought_nothing_does_not_spend_the_step(tmp_path):
    """An IOC that came back empty recovered nothing; the retry - this window
    or a later one - must be sized as the step again."""
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    proposal(store, DAY + M, 4, 2, status="unfilled")
    assert store.upsized_since(DAY) is False
    assert main.loss_step_size(store, STEP, 2, 0.75)[0] == 4


def test_a_failed_order_call_still_spends_the_step(tmp_path):
    """'failed' is an exception from the order call - the order may exist, so
    the step is not offered twice on it."""
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    proposal(store, DAY + M, 4, 2, status="failed")
    assert store.upsized_since(DAY) is True


def test_when_the_budget_caps_the_tier_the_step_is_judged_against_that_base(tmp_path):
    """/autosize below the ask makes the order-path base 1 on a tier-2 day.
    The step sizes 2 from it, and judged against the tier (2) that read as no
    upsize - so it fired on every in-band trade of the episode."""
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    count, why, base, is_recovery, _, due = main.recovery_sizing(
        store, STEP, 1, "", 0.79)
    # Nothing is upsized any more: the step only says a recovery is due.
    assert (count, base, is_recovery, due) == (1, 1, False, True), why
    # ...and the combo that recovery became spends it.
    combo = store.create_proposal("combo_recovery", DAY + M, "KXMVE-T", "UP",
                                  0.56, 0, 1, DAY + M + 60_000, DAY + 2 * M,
                                  DAY + M, base_count=1)
    store.finish_proposal(combo.id, "filled", "")
    assert main.recovery_sizing(store, STEP, 1, "", 0.79)[1] == \
        "recovery already taken for this loss"


def test_a_recorded_base_is_used_whatever_the_capital_flag(tmp_path):
    store = tiered(tmp_path, 2)
    pid = proposal(store, DAY, 2, 2)
    assert store.entry_was_upsized(pid, tiered=False) is False
    assert store.entry_was_upsized(pid, tiered=True) is False


# ------------------------------------------- the order path, executed

def test_a_due_recovery_stays_at_base_size_and_asks_for_a_combo(tmp_path):
    """Operator, 2026-09-27: "replace the single recover into a Combo with
    same base size, no more up scaling"."""
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    count, why, base, is_recovery, _, due = main.recovery_sizing(
        store, STEP, 2, "", 0.75)
    assert (count, base, is_recovery, due) == (2, 2, False, True)
    assert why == "recovery due at 0.75 - combo at base size 2"
    off = Settings(loss_step_enabled=True, loss_step_budget=2.0,
                   recovery_combo_enabled=False)
    assert main.recovery_sizing(store, off, 2, "", 0.75)[0] == 2
    assert main.recovery_sizing(store, off, 2, "", 0.75)[5] is False


def test_an_ordinary_entry_is_not_marked_as_a_recovery(tmp_path):
    store = tiered(tmp_path, 2)
    entry(store, "win", DAY, 2, won=True)
    first = main.recovery_sizing(store, STEP, 2, "", 0.75)
    assert (first[2], first[3], first[5]) == (2, False, False)
    entry(store, "loss", DAY + M, 2, won=False)
    count, why, base, is_recovery, _, due = main.recovery_sizing(
        store, STEP, 2, "", 0.88)
    assert (count, base, is_recovery, due) == (2, 2, False, False)
    assert why.startswith("recovery waiting for 0.70-0.79")


def test_primary_signal_is_wired_to_the_sizing_it_was_given():
    """Nothing that calls `primary_signal` has a trader, so its wiring is
    pinned here: the mark comes from `recovery_sizing`, and the proposal
    records the base it returned."""
    import inspect

    source = inspect.getsource(main.primary_signal)
    flat = " ".join(source.split())
    # The base handed in is the count the base rule produced - not a literal.
    assert ("(count, size_reason, base_count, is_recovery, recovery_reason, "
            "combo_due) = recovery_sizing( store, settings, count, "
            "size_reason, contract_ask )") in flat
    assert "trader.entry_is_recovery = is_recovery" in source
    # The combo is tried BEFORE the single-leg proposal, with the base count,
    # and a True answer ends the window's order path.
    combo = flat.index("stop, note = await place_combo_recovery(")
    single = flat.index("proposal = create_proposal(")
    assert combo < single
    assert ("place_combo_recovery( store, settings, telegram, trader, contract, "
            "prediction.side, contract_ask, base_count, opened, remaining, "
            "now_ms, ) if stop: return") in flat
    create = source[source.index('proposal = create_proposal('):]
    assert "base_count=base_count" in create[:400]


def test_the_armed_message_is_given_todays_base_and_the_add_on():
    import inspect

    source = inspect.getsource(main.service)
    call = source[source.index("messages.recovery_armed_message("):]
    call = call[:call.index("elif event ==")]
    assert "base=(" in call and "capital.base_contracts(now_ms)" in call
    assert "add_per_base=(" in call and "recovery_add_max_contracts" in call


def test_the_armed_message_is_given_the_combo_and_its_partner():
    import inspect

    source = inspect.getsource(main.service)
    call = source[source.index("messages.recovery_armed_message("):]
    call = call[:call.index("elif event ==")]
    assert "combo=(settings.recovery_combo_enabled" in call
    assert "combo_recovery.PARTNERS" in call


# ---------------------------------------------------- the add, end to end

def _deficit_of_two_forty(store, harness):
    """A second realised loss: $2.40 over 4 steps is $0.60 a trade. One
    contract plus a 1-contract add can make about $0.45 at these prices;
    two plus two can make about $0.91. So the decision turns on the counts."""
    store.record_realised(
        "KXBTC15M-PRIOR2",
        harness.NOW - (harness.NOW % 86_400_000) + 3_700_000, -1.60, False,
        "exchange", harness.NOW,
    )
    store.db.commit()
    assert round(store.recovery_state(4).required_per_trade(), 6) == 0.6


def _base_2_harness(tmp_path):
    import test_recovery_add_runner as harness

    settings, store = harness.make(tmp_path)
    store.db.execute("UPDATE trade_proposals SET count = 2 WHERE id = 'p1'")
    store.record_capital_day(Capital(
        ny_day=ny_day(harness.NOW), reconciled_cash=79.61,
        open_exposure=0.0, base_contracts=2, account_ceiling=30.0,
        reconciled_ms=harness.NOW - 1))
    store.db.commit()
    return harness, settings, store


def test_the_runner_judges_the_add_on_the_real_counts(tmp_path):
    harness, settings, store = _base_2_harness(tmp_path)
    _deficit_of_two_forty(store, harness)
    trader = harness.FakeTrader()
    harness.run(RecoveryAddRunner(settings, store), trader)
    assert [p["count"] for p in trader.placed] == [2]


def test_at_base_1_the_same_deficit_is_out_of_reach(tmp_path):
    """The control: the same deficit at base 1 is refused on the combined
    average, exactly as before this change."""
    import test_recovery_add_runner as harness

    settings, store = harness.make(tmp_path)
    _deficit_of_two_forty(store, harness)
    trader = harness.FakeTrader()
    harness.run(RecoveryAddRunner(settings, store), trader)
    assert trader.placed == []
    row = store.open_add(harness.WINDOW)
    assert "recovery needs +0.6000" in row["cancel_reason"]


def test_the_money_reserved_is_for_every_contract_the_add_rests(tmp_path):
    """2 x 0.75 + the 2-contract fee = 1.5263. A balance of 1.52 passes the
    cap (which excludes the fee) and must fail the reservation; reserving for
    one contract, or charging a one-contract fee, would have let it through."""
    harness, settings, store = _base_2_harness(tmp_path)
    claim = round(2 * 0.75 + kalshi_fee_charged(0.75, 2), 6)
    assert claim == 1.5263
    trader = harness.FakeTrader()
    trader.balance = 1.52
    harness.run(RecoveryAddRunner(settings, store), trader)
    assert trader.placed == []
    assert f"could not reserve {claim:.4f}" in store.open_add(
        harness.WINDOW)["cancel_reason"]


def test_a_base_2_entry_that_filled_1_gets_an_add_of_1(tmp_path):
    store = tiered(tmp_path, 2)
    pid = proposal(store, DAY, 2, 2, filled=1)
    runner = RecoveryAddRunner(Settings(), store)
    assert runner.add_count(pid, held=1) == 1
    assert runner.add_count(pid, held=2) == 2


# ---------------------------------------------------- stand-down edges

def test_with_the_step_off_nothing_stands_the_add_down(tmp_path):
    store = tiered(tmp_path, 2)
    pid = proposal(store, DAY, 4, 2)
    off = Settings(loss_step_enabled=False)
    assert main.add_on_stands_down(store, off, None, ("UP", 0.75, 4, "T", pid)) is False
    assert main.add_on_stands_down(store, STEP, None, ("UP", 0.75, 4, "T", pid)) is True


# ------------------------------------------------------------ migration

def test_an_old_database_gains_the_columns_and_its_rows_still_read(tmp_path):
    """A live database predates `base_count`/`ordered_count`. Opening it must
    add them without losing a row, and its rows fall back to the tier."""
    import sqlite3

    path = str(tmp_path / "old.db")
    store = Store(path)
    entry(store, "legacy", DAY, 2)
    store.db.close()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE trade_proposals DROP COLUMN base_count")
    raw.execute("ALTER TABLE trade_proposals DROP COLUMN ordered_count")
    raw.commit()
    raw.close()

    reopened = Store(path)
    columns = {r[1] for r in reopened.db.execute(
        "PRAGMA table_info(trade_proposals)")}
    assert {"base_count", "ordered_count"} <= columns
    assert reopened.entry_base("legacy") == 1        # no review: tier 1
    assert reopened.entry_was_upsized("legacy") is True


# ------------------------------- once per episode, never back to back
#
# Operator, 2026-09-27: "remove the back to back, it should only happen once".
# That afternoon SOL lost an ordinary trade at 15:30, the step fired 4 at
# 16:00 and lost, the loss re-armed it, and the 16:15 step lost 4 again.

def test_a_losing_recovery_trade_does_not_arm_another(tmp_path):
    store = tiered(tmp_path, 2)
    entry(store, "ordinary-loss", DAY, 2, won=False)            # 15:30
    entry(store, "recovery-loss", DAY + M, 4, won=False)        # 16:00, the step
    count, why = main.loss_step_size(store, STEP, 2, 0.75)      # 16:15
    assert (count, why) == (2, "last loss was the recovery trade itself - no second recovery")
    assert main.recovery_sizing(store, STEP, 2, "", 0.75)[5] is False, \
        "and no second recovery combo is due"


def test_the_same_holds_when_the_base_was_recorded(tmp_path):
    store = tiered(tmp_path, 2)
    entry(store, "ordinary-loss", DAY, 2, won=False)
    pid = proposal(store, DAY + M, 4, 2)
    store.db.execute(
        "INSERT INTO settlements (ticker, event_ticker, market_result, yes_count,"
        " yes_cost, no_count, no_cost, revenue_cents, fee_cost, pnl, settled_ms,"
        " synced_at, window_ms) VALUES (?,?,'no',0,0,0,0,0,0,-3.0,?,?,?)",
        (f"T-{DAY + M}", f"T-{DAY + M}", DAY + 2 * M, DAY + M, DAY + M))
    store.db.execute("UPDATE trade_proposals SET fill_price=0.75, fee_paid=0.05 "
                     "WHERE id=?", (pid,))
    store.db.commit()
    assert store.window_was_upsized(DAY + M) is True
    assert main.loss_step_size(store, STEP, 2, 0.75)[0] == 2


def test_the_next_ordinary_loss_arms_the_step_again(tmp_path):
    store = tiered(tmp_path, 2)
    entry(store, "ordinary-loss", DAY, 2, won=False)
    entry(store, "recovery-loss", DAY + M, 4, won=False)
    entry(store, "win", DAY + 2 * M, 2, won=True)
    entry(store, "new-loss", DAY + 3 * M, 2, won=False)         # a new episode
    count, why = main.loss_step_size(store, STEP, 2, 0.75)
    assert count == 4, why


def test_a_winning_recovery_then_an_ordinary_loss_arms_normally(tmp_path):
    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    entry(store, "recovery-win", DAY + M, 4, won=True)
    entry(store, "loss-2", DAY + 2 * M, 2, won=False)
    assert main.loss_step_size(store, STEP, 2, 0.72)[0] == 4


def test_at_base_1_a_losing_recovery_does_not_re_arm_either(tmp_path):
    store = tiered(tmp_path, 1)
    entry(store, "loss", DAY, 1, won=False)
    entry(store, "recovery-loss", DAY + M, 2, won=False)
    assert main.loss_step_size(store, STEP, 1, 0.75)[0] == 1
