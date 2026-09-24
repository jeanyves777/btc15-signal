"""Settled markets are not open positions, and must not size the account up.

THE OPERATOR ASKED why BTC was buying two contracts when the account was in
one-contract range. It was:

    capital_for_tier = settled cash + open exposure
    tier             = capital // capital_per_contract, capped at 2

and `open_position_cost` summed EVERY row whose status was filled /
protected / unprotected, with no time or settlement filter.
`trade_proposals.status` does not reliably reach a terminal value - the
RUNBOOK and FINDINGS 34 both say so - so rows stay at `filled` for ever.

On 2026-09-24 that was 102 rows and $141.25 of "committed capital", of which
$139.51 had already been settled and PAID by the broker, the oldest three
days earlier. $1.74 was genuinely open.

The phantom exposure grew about $1.70 every time the bot traded, so the tier
ratcheted up with TRADE COUNT rather than money:

    09-22  capital  32.03 = cash 32.03 + exposure   0.00  -> tier 1
    09-23  capital  82.99 = cash 34.25 + exposure  48.74  -> tier 2
    09-24  capital 154.80 = cash 35.32 + exposure 119.48  -> tier 2

Settled cash never moved. The size doubled anyway.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.capital import tier_for  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

W = 1_790_000_000_000


def proposal(store, ticker, count, price, window, status="filled"):
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side,"
        " entry_limit, take_profit, count, expires_at, close_ms, status,"
        " created_at, fill_price) "
        "VALUES (?,'primary',?,?,'UP',?,0,?,?,?,?,?,?)",
        (f"p-{ticker}", window, ticker, price, count, window + 60_000,
         window + 900_000, status, window, price))
    store.db.commit()


def settle(store, ticker, window):
    store.db.execute(
        "INSERT INTO settlements (ticker, event_ticker, market_result,"
        " yes_count, yes_cost, no_count, no_cost, revenue_cents, fee_cost,"
        " pnl, settled_ms, synced_at, window_ms) "
        "VALUES (?,?,'yes',0,0,0,0,0,0,0.1,?,?,?)",
        (ticker, ticker, window + 900_000, window + 900_000, window))
    store.db.commit()


# --------------------------------------------------------------- the defect

def test_a_settled_market_is_not_open_exposure(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    proposal(store, "T-OLD", 2, 0.85, W)
    assert store.open_position_cost() == 1.70
    settle(store, "T-OLD", W)
    assert store.open_position_cost() == 0.0


def test_a_live_position_still_counts(tmp_path):
    """The fix must not zero real exposure - that would shrink the tier
    every time a trade was on, which is the defect the cost basis exists to
    prevent in the first place."""
    store = Store(str(tmp_path / "b.db"))
    proposal(store, "T-LIVE", 2, 0.90, W)
    assert store.open_position_cost() == 1.80


def test_a_closed_but_unsettled_market_still_counts(tmp_path):
    """The money is committed until it pays out. Kalshi settles in batches
    hours after close, and treating that gap as free capital would be the
    same error in the other direction."""
    store = Store(str(tmp_path / "c.db"))
    proposal(store, "T-PENDING", 2, 0.80, W - 7_200_000)   # closed 2h ago
    assert store.open_position_cost() == 1.60


def test_history_does_not_accumulate(tmp_path):
    """THE ONE THAT MATTERS. A hundred settled trades must contribute
    nothing, however long their status rows sit at `filled`."""
    store = Store(str(tmp_path / "d.db"))
    for i in range(100):
        t = f"T{i}"
        proposal(store, t, 2, 0.85, W + i * 900_000)
        settle(store, t, W + i * 900_000)
    proposal(store, "T-LIVE", 2, 0.87, W + 200 * 900_000)
    assert store.open_position_cost() == 1.74


# ------------------------------------------------------- what it meant live

def test_the_tier_returns_to_one_on_the_real_numbers():
    """cash 35.32 is the settled figure from the 09-24 review."""
    cash, per_contract, ceiling = 35.32, 30.0, 2
    assert tier_for(cash + 119.48, per_contract, ceiling) == 2   # as computed
    assert tier_for(cash + 1.74, per_contract, ceiling) == 1     # corrected


def test_the_tier_floor_is_still_one():
    """An empty account still trades one contract; it never returns zero."""
    assert tier_for(0.0, 30.0, 2) == 1
    assert tier_for(-5.0, 30.0, 2) == 1


def test_the_ceiling_still_binds():
    assert tier_for(10_000.0, 30.0, 2) == 2


# --------------------------------------------------- the query, not the flag

def test_it_filters_on_the_brokers_settlement_record():
    """Not on the local status column, which is what failed. The broker's
    settlement record is the authority wherever money is concerned."""
    import inspect

    source = inspect.getsource(Store.open_position_cost)
    assert "FROM settlements" in source
    assert "NOT EXISTS" in source
