"""The loss step: $2 after a losing market, base after a win.

THE OPERATOR'S RULE, 2026-09-24, shipped on their decision with the evidence
against it recorded beside it (config.py, FINDINGS 61). What these tests pin
is that what runs is what was MEASURED - a step keyed on the previous
market's result, bounded, never stacking, and never able to create a trade.

The measurement sized the WHOLE position from the budget. So the tests that
matter most are the last two groups: the cap holds whatever the price does,
and the conditional add-on stands down when this fires. Two sizing rules that
each look bounded is how a cap gets exceeded by their sum.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import btc15_signal.main as main  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

W = 1_790_193_600_000


def store_with(tmp_path, events, name="s.db"):
    """events: [(ticker, window_open, bot_leg_pnl)].

    THE BOT'S OWN TRADES, because that is what the rule keys on. These used to
    write `realised_events`, which holds one row per TICKER and therefore
    blends every contract the ACCOUNT traded in that market - the operator's
    manual fills included. See the 2026-09-24 case at the bottom of this file.
    """
    store = Store(str(tmp_path / name))
    for i, (ticker, window, amount) in enumerate(events):
        won = amount > 0
        store.db.execute(
            "INSERT INTO trade_proposals (id, strategy, window_open, ticker,"
            " side, entry_limit, take_profit, count, expires_at, close_ms,"
            " status, created_at, fill_price, fee_paid) "
            "VALUES (?,'primary',?,?,'UP',0.80,0,2,?,?,'filled',?,0.80,0.02)",
            (f"p{i}", window, ticker, window + 60_000, window + 900_000,
             window))
        store.db.execute(
            "INSERT INTO settlements (ticker, event_ticker, market_result,"
            " yes_count, yes_cost, no_count, no_cost, revenue_cents, fee_cost,"
            " pnl, settled_ms, synced_at, window_ms) "
            "VALUES (?,?,?,0,0,0,0,0,0,?,?,?,?)",
            (ticker, ticker, "yes" if won else "no", amount,
             window + 900_000, window, window))
    store.db.commit()
    return store


def _settle(store, store_id, window, pnl):
    """One further settled BOT market at BASE size.

    count=1 deliberately. `store_with` writes count=2, and the amended rule
    reads `trade_proposals` for "has the step already been spent" - so an
    intervening market written at 2 contracts would look like the upsize had
    already fired and every waiting test would pass for the wrong reason.
    """
    won = pnl > 0
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker,"
        " side, entry_limit, take_profit, count, expires_at, close_ms,"
        " status, created_at, fill_price, fee_paid) "
        "VALUES (?,'primary',?,?,'UP',0.80,0,1,?,?,'filled',?,0.80,0.01)",
        (store_id, window, store_id, window + 60_000, window + 900_000,
         window))
    store.db.execute(
        "INSERT INTO settlements (ticker, event_ticker, market_result,"
        " yes_count, yes_cost, no_count, no_cost, revenue_cents, fee_cost,"
        " pnl, settled_ms, synced_at, window_ms) "
        "VALUES (?,?,?,0,0,0,0,0,0,?,?,?,?)",
        (store_id, store_id, "yes" if won else "no", pnl,
         window + 900_000, window, window))
    store.db.commit()


# ------------------------------------------------------- reading the ledger

def test_no_history_is_not_a_loss(tmp_path):
    """None, not False. A fresh database has not seen a win either."""
    store = store_with(tmp_path, [])
    assert store.last_market_lost() is None


def test_a_losing_market_reads_as_a_loss(tmp_path):
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    assert store.last_market_lost() is True


def test_a_winning_market_resets_it(tmp_path):
    store = store_with(tmp_path, [("A", 1_000_000, -0.80), ("B", 2_000_000, +0.19)])
    assert store.last_market_lost() is False


def test_the_MOST_RECENT_market_decides_not_the_sum(tmp_path):
    """A win after two big losses resets the step, even deep underwater."""
    store = store_with(
        tmp_path, [("A", 1_000_000, -3.00), ("B", 2_000_000, -3.00), ("C", 3_000_000, +0.10)])
    assert store.last_market_lost() is False


def test_a_market_the_bot_never_filled_is_skipped(tmp_path):
    """An unfilled proposal is not a trade and cannot arm the step."""
    store = store_with(tmp_path, [("A", 1_000_000, +0.19)])
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side,"
        " entry_limit, take_profit, count, expires_at, close_ms, status,"
        " created_at) VALUES ('later','primary',?, 'B','DOWN',0.68,0,1,?,?,"
        "'pending',?)",
        (2_000_000, 2_060_000, 2_900_000, 2_000_000))
    store.db.execute(
        "INSERT INTO settlements (ticker, event_ticker, market_result,"
        " yes_count, yes_cost, no_count, no_cost, revenue_cents, fee_cost,"
        " pnl, settled_ms, synced_at, window_ms) "
        "VALUES ('B','B','yes',10,4.8,20,11.1,0,0.35,-6.2492,?,?,?)",
        (2_900_000, 2_900_000, 2_000_000))
    store.db.commit()
    assert store.last_market_lost() is False


# --------------------------------------------------------------- the sizing

def test_it_sizes_to_the_budget_after_a_loss(tmp_path):
    """At an IN-BAND ask. Since 2026-09-25 the upsize waits for 0.70-0.79, so
    the budget arithmetic is still the rule but 0.82 is no longer where it
    applies - see `test_it_waits_when_the_ask_is_too_high`."""
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0)
    count, why = main.loss_step_size(store, s, 1, 0.75)
    assert count == 6, why           # int(5.00 / 0.75)
    assert "recovery taken at 0.75" in why


def test_it_waits_when_the_ask_is_too_high(tmp_path):
    """THE AMENDMENT. The operator's objection: an extra contract at 0.90 risks
    90c to make 10c, which base size would have earned anyway at a better
    price. So the step stays armed and this trade goes out at base - and the
    reason must not read as an upsize."""
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0)
    count, why = main.loss_step_size(store, s, 1, 0.88)
    assert count == 1
    assert "waiting" in why and "0.70-0.79" in why
    assert "taken" not in why


def test_it_survives_a_win_and_fires_later(tmp_path):
    """THE TEST THAT CATCHES THE OBVIOUS WRONG IMPLEMENTATION. "Recovery can
    happen 3 to 5 trades later" only means anything if the armed step outlives
    the markets in between - and since the win rate is about 3 in 4, the very
    next market usually WINS. A first cut of this change returned base whenever
    the last market had won, which killed the feature while every other test
    here still passed."""
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0)
    # a WIN lands after the loss, at a price the step would not have taken
    _settle(store, store_id="win1", window=1_100_000, pnl=+0.19)
    assert main.loss_step_size(store, s, 1, 0.88)[0] == 1, "still waiting"
    count, why = main.loss_step_size(store, s, 1, 0.74)
    assert count > 1, why
    assert "1 market(s) after the loss" in why


def test_it_expires_unspent_after_the_wait(tmp_path):
    """A wait with no bound is not a wait, it is a standing upsize looking for
    a cheap ask - and the further from the loss it fires, the less it recovers
    anything."""
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0,
                 loss_step_wait_markets=3)
    for i in range(3):
        _settle(store, store_id=f"w{i}", window=1_100_000 + i * 100_000,
                pnl=+0.19)
    count, why = main.loss_step_size(store, s, 1, 0.74)
    assert count == 1
    assert "expired unspent" in why


def test_it_fires_once_per_losing_episode(tmp_path):
    """Without this, every later in-band trade inside the waiting window would
    upsize again on ONE loss. Derived from `trade_proposals` rather than a
    flag, so an order that failed after a flag was written cannot hide it."""
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0)
    assert main.loss_step_size(store, s, 1, 0.74)[0] > 1
    # the upsized entry actually went out
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side,"
        " entry_limit, take_profit, count, expires_at, close_ms, status,"
        " created_at) VALUES ('taken','primary',?, 'T','UP',0.74,0,2,?,?,"
        "'filled',?)",
        (1_100_000, 1_160_000, 1_900_000, 1_100_000))
    store.db.commit()
    count, why = main.loss_step_size(store, s, 1, 0.74)
    assert count == 1
    assert "already taken" in why


def test_it_does_nothing_after_a_win(tmp_path):
    store = store_with(tmp_path, [("A", 1_000_000, +0.19)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0)
    assert main.loss_step_size(store, s, 1, 0.82) == (1, "")


def test_it_does_nothing_with_no_history(tmp_path):
    store = store_with(tmp_path, [])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0)
    assert main.loss_step_size(store, s, 1, 0.82) == (1, "")


def test_consecutive_losses_do_not_escalate(tmp_path):
    """A step, not a martingale. The second loss is sized exactly like the
    first - which is what the backtest charged for it, and the reason the
    exposure is bounded whatever the streak length."""
    st = Settings(loss_step_enabled=True, loss_step_budget=5.0)
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    first = main.loss_step_size(store, st, 1, 0.75)[0]
    # a SECOND losing bot trade, after the first
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side,"
        " entry_limit, take_profit, count, expires_at, close_ms, status,"
        " created_at, fill_price, fee_paid) "
        "VALUES ('p9','primary',?, 'B','UP',0.80,0,2,?,?,'filled',?,0.80,0.02)",
        (2_000_000, 2_060_000, 2_900_000, 2_000_000))
    store.db.execute(
        "INSERT INTO settlements (ticker, event_ticker, market_result,"
        " yes_count, yes_cost, no_count, no_cost, revenue_cents, fee_cost,"
        " pnl, settled_ms, synced_at, window_ms) "
        "VALUES ('B','B','no',0,0,0,0,0,0,-1.62,?,?,?)",
        (2_900_000, 2_000_000, 2_000_000))
    store.db.commit()
    assert store.last_market_lost() is True
    second = main.loss_step_size(store, st, 1, 0.75)[0]
    assert first == second == 6


def test_the_flag_turns_it_off(tmp_path):
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    s = Settings(loss_step_enabled=False, loss_step_budget=5.0)
    assert main.loss_step_size(store, s, 1, 0.82) == (1, "")


def test_it_never_sizes_DOWN(tmp_path):
    """It is an upsize or it is nothing. A base already above the budget's
    reach is left alone, with an empty reason so no line claims an upsize."""
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=1.0)
    assert main.loss_step_size(store, s, 4, 0.75) == (4, "")


# ------------------------------------------------------------------ the cap

def test_a_mispriced_ask_cannot_produce_a_position(tmp_path):
    """A stale or mispriced ask is what turns a dollar budget into a position
    nobody chose - $5 at 1c would be 500 contracts. Since 2026-09-25 the band
    refuses it outright, which is STRONGER than the cap: 1c is not 0.70-0.79,
    so the answer is base size and the cap is never reached."""
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0,
                 loss_step_max_contracts=8)
    assert main.loss_step_size(store, s, 1, 0.01)[0] == 1


def test_the_cap_still_binds_inside_the_band(tmp_path):
    """The cap is not decoration just because the band narrowed. Within
    0.70-0.79 a large budget must still be bounded, because the budget is a
    dollar figure and the count it buys is not something anyone typed."""
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=20.0,
                 loss_step_max_contracts=8)
    count, why = main.loss_step_size(store, s, 1, 0.70)
    assert count == 8, why           # int(20 / 0.70) = 28, capped


def test_the_cap_holds_across_the_whole_price_band(tmp_path):
    store = store_with(tmp_path, [("A", 1_000_000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0,
                 loss_step_max_contracts=8)
    # Out-of-band asks return BASE now, which still satisfies the cap - the
    # point of the sweep is that no price produces a position nobody chose.
    for ask in (0.70, 0.75, 0.79, 0.80, 0.85, 0.90, 0.93, 0.95):
        count = main.loss_step_size(store, s, 1, ask)[0]
        assert 1 <= count <= 8, (ask, count)
        assert count * ask <= 5.0 + ask, (ask, count)


def test_the_shipped_settings_are_the_decided_ones():
    """$5 on 2026-09-24, reduced to $2 the same day: "5 is too risky just to
    make 50". The operator's judgement was about a ratio the backtest never
    addressed - and the drawdown was the least-evidenced number in that result,
    since the sample held two 2-loss runs and no 3-loss run."""
    s = Settings()
    assert s.loss_step_enabled is True
    assert s.loss_step_budget == 2.00
    assert s.loss_step_max_contracts == 8
    # The 2026-09-25 amendment, as instructed: wait for 0.70-0.79, up to five
    # settled markets. The evidence against the band is recorded beside it in
    # config.py - the decision is the operator's and sizing always is.
    assert (s.loss_step_band_lo, s.loss_step_band_hi) == (0.70, 0.79)
    assert s.loss_step_wait_markets == 5


def test_two_dollars_cannot_buy_a_large_position():
    """The point of the reduction: at every price in the band, a post-loss
    trade is a small number of contracts rather than a step-change in risk."""
    from btc15_signal.main import contracts_for_budget
    for ask in (0.70, 0.80, 0.93):
        assert contracts_for_budget(2.00, ask) <= 3, ask


# ----------------------------------------------- it cannot stack, or create

def test_the_add_on_stands_down_when_the_step_fires():
    """THE STACKING GUARD. `recovery_add_enabled` is True in the deployed
    .env, so without this the account would hold the full stepped position
    PLUS a rested extra contract - a size nobody chose and nothing measured."""
    import inspect

    source = inspect.getsource(main.service)
    at = source.index("stood_down = bool(")
    window = source[at - 900:at + 300]
    assert "loss_step_enabled" in window
    # Keyed on the step ACTUALLY firing, not on "did the last market lose".
    # Since the step waits for 0.70-0.79, a loss no longer implies an upsize,
    # and standing the add-on down for one that never happens would remove one
    # mechanism without engaging the other.
    assert "loss_step_size(" in window
    assert "stepped_now > 1" in window
    # and the add-on call is actually guarded by it
    call = source.index("await recovery_add.step(")
    assert "not stood_down" in source[:call][-400:]


def test_the_step_is_applied_after_every_other_sizing_rule():
    """Single authority: when it fires it REPLACES the count. If it ran before
    confidence_size or recovery_size, one of those could raise the number
    again on top of a budget that was already the whole position."""
    import inspect

    source = inspect.getsource(main.primary_signal)
    assert source.index("confidence_size(") < source.index("loss_step_size(")
    assert source.index("recovery_size(") < source.index("loss_step_size(")


def test_it_cannot_create_a_trade():
    """It only ever returns a COUNT. Nothing in its CODE reaches an order, a
    gate or a proposal - the same standard sizing has been held to since the
    intelligence layer was denied it. The docstring is stripped first: prose
    about orders is not a call to one."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(main.loss_step_size).strip())
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]
    code = ast.unparse(fn)
    for forbidden in ("execute", "create_proposal", "rule_match", "order",
                      "place", "submit", "trader"):
        assert forbidden not in code, forbidden


# ------------------------- the bot's OWN trade, not the account's market

def proposal(store, *, window, ticker, side, count, fill, result,
             status="filled", exit_price=None, exit_count=None):
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side,"
        " entry_limit, take_profit, count, expires_at, close_ms, status,"
        " created_at, fill_price, fee_paid, exit_price, exit_count) "
        "VALUES (?,'primary',?,?,?,?,0,?,?,?,?,?,?,0.02,?,?)",
        (f"p{window}", window, ticker, side, fill, count, window + 60_000,
         window + 900_000, status, window, fill, exit_price, exit_count))
    store.db.execute(
        "INSERT INTO settlements (ticker, event_ticker, market_result,"
        " yes_count, yes_cost, no_count, no_cost, revenue_cents, fee_cost,"
        " pnl, settled_ms, synced_at, window_ms) "
        "VALUES (?,?,?,0,0,0,0,0,0,?,?,?,?)",
        (ticker, ticker[:-3], result, 0.0, window + 900_000, window, window))
    store.db.commit()


def test_a_market_the_bot_never_filled_does_not_arm_the_step(tmp_path):
    """2026-09-24, KXBTC15M-26SEP241000-00. The bot's only proposal there was
    1 contract DOWN at 0.68 and it stayed PENDING. The operator traded the
    same market by hand - three 10-contract fills at 0.48-0.58, outside every
    gate - and the ticker netted -6.2492. Keyed on the market, the next BOT
    entry would have upsized to $5 on a loss that was not the bot's."""
    store = Store(str(tmp_path / "m.db"))
    proposal(store, window=1_000_000, ticker="T-WIN", side="UP", count=2,
             fill=0.88, result="yes")
    # the manual market: a settlement row and a proposal that never filled
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side,"
        " entry_limit, take_profit, count, expires_at, close_ms, status,"
        " created_at) VALUES ('manual','primary',?, 'T-MANUAL','DOWN',0.68,0,"
        "1,?,?,'pending',?)",
        (2_000_000, 2_060_000, 2_900_000, 2_000_000))
    store.db.execute(
        "INSERT INTO settlements (ticker, event_ticker, market_result,"
        " yes_count, yes_cost, no_count, no_cost, revenue_cents, fee_cost,"
        " pnl, settled_ms, synced_at, window_ms) "
        "VALUES ('T-MANUAL','T-MAN','yes',10,4.8,20,11.1,0,0.35,-6.2492,?,?,?)",
        (2_900_000, 2_900_000, 2_000_000))
    store.db.commit()
    # The bot's last FILLED trade won, so the step must not arm.
    assert store.last_market_lost() is False


def test_it_reads_the_bots_own_leg_not_the_ticker_total(tmp_path):
    """Same market, bot leg wins, ticker total is deeply negative."""
    store = Store(str(tmp_path / "n.db"))
    proposal(store, window=1_000_000, ticker="T1", side="UP", count=2,
             fill=0.80, result="yes")
    store.db.execute("UPDATE settlements SET pnl = -9.99 WHERE ticker='T1'")
    store.db.commit()
    assert store.last_market_lost() is False


def test_a_losing_bot_leg_still_arms_it(tmp_path):
    store = Store(str(tmp_path / "o.db"))
    proposal(store, window=1_000_000, ticker="T1", side="UP", count=2,
             fill=0.80, result="no")
    assert store.last_market_lost() is True


def test_an_early_exit_is_priced_at_the_exit(tmp_path):
    """A position sold at 0.997 on a market that then settled against us is a
    WIN, and must not arm the step."""
    store = Store(str(tmp_path / "p.db"))
    proposal(store, window=1_000_000, ticker="T1", side="UP", count=2,
             fill=0.80, result="no", status="exited",
             exit_price=0.997, exit_count=2)
    assert store.last_market_lost() is False
