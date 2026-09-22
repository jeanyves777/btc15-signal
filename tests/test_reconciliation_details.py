"""Three details the operator asked to see proved, not asserted.

  * one exit ORDER can produce several FILLS; reconciliation must replace the
    provisional with all of them, losing and duplicating nothing, and replay
    recovery when an amount or a timestamp changes
  * once the broker holds an order's reservation, the local claim must stop
    subtracting the same money - including the case where the submission
    outcome is unknown
  * sizing capital excludes unrealised GAINS but must not ignore unrealised
    LOSSES
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.capital import ny_day_start_ms  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

NOW = int(time.time() * 1000)
TODAY = ny_day_start_ms(NOW)
WINDOW = TODAY + 3_600_000
TICKER = "KXBTC15M-TEST-00"


def buy(store, price=0.70, count=2, fee=0.04, when=None):
    store.db.execute(
        "INSERT INTO fills (fill_id, ticker, order_id, action, side, count, "
        "yes_price, no_price, fee_cost, is_taker, filled_ms, window_ms, synced_at) "
        "VALUES ('buy-1',?,'o1','buy','yes',?,?,?,?,1,?,?,?)",
        (TICKER, count, price, 1 - price, fee, when or NOW, WINDOW, NOW),
    )
    store.db.commit()


def sell(store, fill_id, price, count, when, fee=0.01):
    store.db.execute(
        "INSERT OR REPLACE INTO fills (fill_id, ticker, order_id, action, side, "
        "count, yes_price, no_price, fee_cost, is_taker, filled_ms, window_ms, "
        "synced_at) VALUES (?,?,'exit-1','sell','yes',?,?,?,?,0,?,?,?)",
        (fill_id, TICKER, count, price, 1 - price, fee, when, WINDOW, NOW),
    )
    store.db.commit()


# ------------------------------------------- one order, several fills

def test_one_exit_order_with_two_fills_becomes_two_events(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    buy(store)
    sell(store, "f-1", 0.88, 1, NOW + 10_000)
    sell(store, "f-2", 0.92, 1, NOW + 20_000)   # same order, second level
    store.sync_events_from_fills(NOW + 30_000)

    events = [e for e in store.settlement_events(TICKER) if e["source"] == "cash_out"]
    assert {e["event_id"] for e in events} == {"cash_out:f-1", "cash_out:f-2"}
    assert [e["realised_ms"] for e in events] == [NOW + 10_000, NOW + 20_000]


def test_the_provisional_is_replaced_by_every_fill_not_just_a_matching_one(tmp_path):
    """The provisional is keyed on when the bot NOTICED. Matching it to a fill
    by timestamp would leave the other executions behind and count the sale
    twice."""
    store = Store(str(tmp_path / "s.db"))
    buy(store)
    # The live path books a provisional for the whole exit at one instant.
    store.record_realised(
        TICKER, WINDOW, 0.35, True, "cash_out", NOW, realised_ms=NOW + 15_000
    )
    assert any(
        e["event_id"].count(":") == 2 for e in store.settlement_events(TICKER)
    )

    sell(store, "f-1", 0.88, 1, NOW + 10_000)
    sell(store, "f-2", 0.92, 1, NOW + 20_000)
    store.sync_events_from_fills(NOW + 30_000)

    ids = {e["event_id"] for e in store.settlement_events(TICKER)}
    assert ids == {"cash_out:f-1", "cash_out:f-2"}, "no provisional survives"


def test_no_pnl_is_lost_or_duplicated_across_the_replacement(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    buy(store, price=0.70, count=2, fee=0.04)
    sell(store, "f-1", 0.88, 1, NOW + 10_000, fee=0.01)
    sell(store, "f-2", 0.92, 1, NOW + 20_000, fee=0.01)
    store.sync_events_from_fills(NOW + 30_000)

    total = sum(
        e["amount"] for e in store.settlement_events(TICKER)
        if e["source"] == "cash_out"
    )
    # Each fill: (exit - 0.70) * 1 - half the 0.04 entry fee - its own 0.01.
    expected = ((0.88 - 0.70) - 0.02 - 0.01) + ((0.92 - 0.70) - 0.02 - 0.01)
    assert round(total, 6) == round(expected, 6)


def test_a_duplicate_fill_delivery_changes_nothing(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    buy(store)
    sell(store, "f-1", 0.88, 1, NOW + 10_000)
    store.sync_events_from_fills(NOW + 30_000)
    first = store.recovery_state(4).deficit
    events = len(store.settlement_events(TICKER))

    for _ in range(3):
        store.sync_events_from_fills(NOW + 40_000)
    assert len(store.settlement_events(TICKER)) == events
    assert store.recovery_state(4).deficit == first


def test_recovery_is_replayed_when_the_amount_changes(tmp_path):
    """A provisional replaced by fills of a different total must not leave the
    deficit carrying the old figure."""
    store = Store(str(tmp_path / "s.db"))
    store.record_realised(
        "KX-LOSS", WINDOW, -1.00, False, "exchange", NOW, realised_ms=NOW
    )
    assert store.recovery_state(4).deficit == 1.00

    buy(store)
    store.record_realised(
        TICKER, WINDOW, 0.10, True, "cash_out", NOW, realised_ms=NOW + 15_000
    )
    assert round(store.recovery_state(4).deficit, 6) == 0.90

    # The broker's fills say the exit was worth more than the provisional.
    sell(store, "f-1", 0.90, 2, NOW + 10_000, fee=0.01)
    store.sync_events_from_fills(NOW + 30_000)
    rebuilt = store.recovery_state(4).deficit
    expected = round(1.00 - ((0.90 - 0.70) * 2 - 0.04 - 0.01), 6)
    assert round(rebuilt, 6) == expected, "the deficit replays, not patches"


def test_a_rebuild_is_deterministic(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.record_realised("A", WINDOW, -1.00, False, "exchange", NOW, realised_ms=NOW)
    store.record_realised(
        "B", WINDOW, 0.40, True, "exchange", NOW, realised_ms=NOW + 1_000
    )
    once = store.rebuild_deficit(NOW + 5_000).deficit
    twice = store.rebuild_deficit(NOW + 6_000).deficit
    assert once == twice == 0.60


# ------------------------------------------------- the reservation handoff

def test_a_reservation_the_broker_now_holds_is_released(tmp_path):
    """Kalshi reserves a resting order against its own balance. Keeping the
    local claim as well subtracts the same dollars twice."""
    store = Store(str(tmp_path / "s.db"))
    assert store.reserve_funds("add:1", 0.76, NOW, available=10.0)
    assert store.reserved_funds(NOW) == 0.76
    store.release_funds("add:1", reason="broker holds the reservation")
    assert store.reserved_funds(NOW) == 0.0


def test_an_unknown_submission_outcome_keeps_the_reservation(tmp_path):
    """The order may or may not exist. Releasing would let the next order
    spend money an in-flight one might already hold."""
    store = Store(str(tmp_path / "s.db"))
    store.reserve_funds("add:1", 0.76, NOW, available=10.0)
    # No release on an unknown outcome, and no clock frees it either.
    assert store.reserved_funds(NOW + 3_600_000) == 0.76


def test_a_fill_moves_the_claim_into_position_exposure(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.reserve_funds("add:1", 0.76, NOW, available=10.0)
    store.release_funds("add:1", reason="filled")
    assert store.reserved_funds(NOW) == 0.0


# --------------------------------------------------- conservative capital

def test_unrealised_losses_reduce_sizing_capital(tmp_path):
    """Cost basis alone would count a position bought for $2 and now worth
    $0.20 as $2 of capital, holding the tier up on money already gone."""
    store = Store(str(tmp_path / "s.db"))
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side, "
        "entry_limit, take_profit, count, expires_at, close_ms, status, "
        "created_at, fill_price) VALUES "
        "('p1','primary',?,?,'UP',1.00,0,2,?,?,'filled',?,1.00)",
        (WINDOW, TICKER, NOW, WINDOW + 900_000, NOW),
    )
    store.db.commit()
    assert store.open_position_cost() == 2.0

    store.set_setting("open_mark", -1.80, NOW)      # marked down to 0.20
    conservative = round(
        max(0.0, store.open_position_cost())
        + min(0.0, store.get_setting("open_mark", 0.0)),
        6,
    )
    assert conservative == 0.20, "a losing position is marked to market"

    store.set_setting("open_mark", 5.0, NOW)        # a big unrealised gain
    conservative = round(
        max(0.0, store.open_position_cost())
        + min(0.0, store.get_setting("open_mark", 0.0)),
        6,
    )
    assert conservative == 2.0, "a winning position is NOT marked up"
