"""The loss step: $5 after a losing market, base after a win.

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


def store_with(tmp_path, events):
    """events: [(ticker, realised_ms, amount)] written to the real ledger."""
    store = Store(str(tmp_path / "s.db"))
    for i, (ticker, ms, amount) in enumerate(events):
        store.db.execute(
            "INSERT INTO realised_events (event_id, ticker, realised_ms, "
            "amount, source, window_ms, applied, recorded_ms) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"e{i}", ticker, ms, amount, "exchange", W, amount, ms),
        )
    store.db.commit()
    return store


# ------------------------------------------------------- reading the ledger

def test_no_history_is_not_a_loss(tmp_path):
    """None, not False. A fresh database has not seen a win either."""
    store = store_with(tmp_path, [])
    assert store.last_market_lost() is None


def test_a_losing_market_reads_as_a_loss(tmp_path):
    store = store_with(tmp_path, [("A", 1000, -0.80)])
    assert store.last_market_lost() is True


def test_a_winning_market_resets_it(tmp_path):
    store = store_with(tmp_path, [("A", 1000, -0.80), ("B", 2000, +0.19)])
    assert store.last_market_lost() is False


def test_the_MOST_RECENT_market_decides_not_the_sum(tmp_path):
    """A win after two big losses resets the step, even deep underwater."""
    store = store_with(
        tmp_path, [("A", 1000, -3.00), ("B", 2000, -3.00), ("C", 3000, +0.10)])
    assert store.last_market_lost() is False


def test_a_partly_cashed_out_market_is_summed_not_split(tmp_path):
    """THE ONE THAT BIT THE BACKTEST. A position sold early writes a cash_out
    row AND an exchange row for the remainder. Either alone is a fraction of
    the result; 10 of 199 live markets are like this."""
    store = Store(str(tmp_path / "c.db"))
    for i, (src, amt) in enumerate((("cash_out", -0.90), ("exchange", +1.05))):
        store.db.execute(
            "INSERT INTO realised_events (event_id, ticker, realised_ms, "
            "amount, source, window_ms, applied, recorded_ms) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"x{i}", "A", 1000 + i, amt, src, W, amt, 1000 + i),
        )
    store.db.commit()
    # -0.90 + 1.05 = +0.15: a WIN. Reading only the last row says loss.
    assert store.last_market_lost() is False


# --------------------------------------------------------------- the sizing

def test_it_sizes_to_the_budget_after_a_loss(tmp_path):
    store = store_with(tmp_path, [("A", 1000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0)
    count, why = main.loss_step_size(store, s, 1, 0.82)
    assert count == 6, why           # int(5.00 / 0.82)
    assert "last market lost" in why


def test_it_does_nothing_after_a_win(tmp_path):
    store = store_with(tmp_path, [("A", 1000, +0.19)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0)
    assert main.loss_step_size(store, s, 1, 0.82) == (1, "")


def test_it_does_nothing_with_no_history(tmp_path):
    store = store_with(tmp_path, [])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0)
    assert main.loss_step_size(store, s, 1, 0.82) == (1, "")


def test_consecutive_losses_do_not_escalate(tmp_path):
    """A step, not a martingale. The second loss is the same size as the
    first - which is exactly what the backtest charged for it."""
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0)
    one = store_with(tmp_path, [("A", 1000, -0.80)])
    first = main.loss_step_size(one, s, 1, 0.80)[0]
    one.db.execute(
        "INSERT INTO realised_events (event_id, ticker, realised_ms, amount, "
        "source, window_ms, applied, recorded_ms) VALUES (?,?,?,?,?,?,?,?)",
        ("e9", "B", 2000, -4.00, "exchange", W, -4.00, 2000),
    )
    one.db.commit()
    second = main.loss_step_size(one, s, 1, 0.80)[0]
    assert first == second == 6


def test_the_flag_turns_it_off(tmp_path):
    store = store_with(tmp_path, [("A", 1000, -0.80)])
    s = Settings(loss_step_enabled=False, loss_step_budget=5.0)
    assert main.loss_step_size(store, s, 1, 0.82) == (1, "")


def test_it_never_sizes_DOWN(tmp_path):
    """It is an upsize or it is nothing. A base already above the budget's
    reach is left alone, with an empty reason so no line claims an upsize."""
    store = store_with(tmp_path, [("A", 1000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=1.0)
    assert main.loss_step_size(store, s, 4, 0.82) == (4, "")


# ------------------------------------------------------------------ the cap

def test_the_cap_binds_at_a_cheap_ask(tmp_path):
    """A stale or mispriced ask is what turns a dollar budget into a position
    nobody chose. $5 at 1c would be 500 contracts."""
    store = store_with(tmp_path, [("A", 1000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0,
                 loss_step_max_contracts=8)
    assert main.loss_step_size(store, s, 1, 0.01)[0] == 8


def test_the_cap_holds_across_the_whole_price_band(tmp_path):
    store = store_with(tmp_path, [("A", 1000, -0.80)])
    s = Settings(loss_step_enabled=True, loss_step_budget=5.0,
                 loss_step_max_contracts=8)
    for ask in (0.70, 0.75, 0.80, 0.85, 0.90, 0.93, 0.95):
        count = main.loss_step_size(store, s, 1, ask)[0]
        assert 1 <= count <= 8, (ask, count)
        assert count * ask <= 5.0 + ask, (ask, count)


def test_the_shipped_settings_are_the_decided_ones():
    s = Settings()
    assert s.loss_step_enabled is True
    assert s.loss_step_budget == 5.00
    assert s.loss_step_max_contracts == 8


# ----------------------------------------------- it cannot stack, or create

def test_the_add_on_stands_down_when_the_step_fires():
    """THE STACKING GUARD. `recovery_add_enabled` is True in the deployed
    .env, so without this the account would hold the full stepped position
    PLUS a rested extra contract - a size nobody chose and nothing measured."""
    import inspect

    source = inspect.getsource(main.service)
    at = source.index("stood_down = bool(")
    window = source[at:at + 300]
    assert "loss_step_enabled" in window
    assert "last_market_lost" in window
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
