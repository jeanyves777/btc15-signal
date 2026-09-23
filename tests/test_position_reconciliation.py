"""Every leg of a position, reconciled by broker order and fill ID.

THE DEFECT, on real money, 2026-09-23, KXBTC15M-26SEP231615-15:

    the recap said   Bought DOWN at 85c / Cost $1.72 / Profit +$0.45
    the broker said  3 NO contracts, no_total_cost_dollars 2.530000

2 x (99.7c - 85c) is 29.4c gross, so +$0.4456 could not come from the fills
the message described. It came from three orders, not two:

    01a0cfde-71e8-...  base      2 @ 0.85  fee 0.0179  taker  20:04:17Z
    01a0cfde-79b8-...  recovery  1 @ 0.83  fee 0.0     maker  20:07:14Z
    01a0cfe6-e9e0-...  exit      2 @ 0.997 fee 0.0005  taker  20:13:32Z

    3 bought for 2.5479 · 2 sold for 1.9935 · 1 settled for 1.00 = +0.4456

ROOT CAUSE. `order_status` read `/portfolio/events/orders/{id}`, which
returns 404 for every order that has ever existed. So it always returned
None, so `_bank_if_filled` never banked anything, so the cancel/fill race it
was written to resolve had never once been resolved - and every recovery add
that filled was written down as CANCELLED, with `cancel_reason` reading
"order not found (already filled, expired or cancelled)".

The ledger stayed correct throughout because it reads
`/portfolio/settlements`. The money was right and the message was wrong,
which is the only ordering of those two that is recoverable.
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal import messages, surface  # noqa: E402
from btc15_signal.execution import KalshiExecutionClient  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

W = 1_790_193_600_000
TICKER = "KXBTC15M-26SEP231615-15"

# The order as Kalshi actually returned it, field for field.
REAL_ORDER = {
    "action": "sell", "book_side": "ask",
    "client_order_id": "9a6960c1-f561-5014-afc8-5276df17d912",
    "created_time": "2026-09-23T20:04:19.528164Z",
    "fill_count_fp": "1.00", "initial_count_fp": "1.00",
    "last_update_time": "2026-09-23T20:07:14.885405Z",
    "maker_fees_dollars": "0.000000", "maker_fill_cost_dollars": "0.830000",
    "no_price_dollars": "0.8300", "outcome_side": "no",
    "order_id": "01a0cfde-79b8-7184-a6f6-b326acb11db1",
    "remaining_count_fp": "0.00", "side": "yes", "status": "executed",
    "taker_fees_dollars": "0.000000", "taker_fill_cost_dollars": "0.000000",
    "ticker": TICKER, "type": "limit", "yes_price_dollars": "0.1700",
}


def plain(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", text)


# --------------------------------------------- the order is read at all

def test_order_status_reads_a_live_endpoint():
    """`/portfolio/events/orders/{id}` 404s for every order. The create and
    cancel calls moved to that family; the READ never existed there."""
    import inspect

    src = inspect.getsource(KalshiExecutionClient.order_status)
    assert '"/portfolio/orders/{order_id}"' in src.replace("f\"", "\"")
    assert "events/orders" not in src.split('"""')[-1]


# ------------------------------------------------ the fields are real

def test_the_fill_is_parsed_from_the_fields_kalshi_returns():
    got = KalshiExecutionClient.parse_fill(REAL_ORDER, "DOWN")
    assert got["count"] == 1.0
    assert got["price"] == 0.83
    assert got["fee"] == 0.0
    assert got["is_taker"] == 0, "it rested and filled as a maker"


def test_the_price_is_our_side_not_the_complement():
    """`yes_price_dollars` is 0.1700 on this order and the fill was 0.8300.
    The old parser fell back to exactly that field."""
    got = KalshiExecutionClient.parse_fill(REAL_ORDER, "DOWN")
    assert got["price"] != 0.17
    assert got["price"] == 0.83


def test_the_price_is_cost_over_count_not_a_quote():
    """Two contracts costing 1.70 is 0.85 each, whatever the book shows now."""
    order = dict(REAL_ORDER, fill_count_fp="2.00",
                 taker_fill_cost_dollars="1.700000",
                 maker_fill_cost_dollars="0.000000",
                 taker_fees_dollars="0.017900",
                 no_price_dollars="0.9900")
    got = KalshiExecutionClient.parse_fill(order, "DOWN")
    assert got["count"] == 2.0
    assert got["price"] == 0.85
    assert got["fee"] == 0.0179
    assert got["is_taker"] == 1


# ------------------------------------ a pending order is not a position

def test_an_unfilled_order_parses_as_no_fill():
    resting = dict(REAL_ORDER, fill_count_fp="0.00",
                   initial_count_fp="1.00", remaining_count_fp="1.00",
                   maker_fill_cost_dollars="0.000000", status="resting")
    assert KalshiExecutionClient.parse_fill(resting, "DOWN") is None


def test_a_partial_fill_counts_only_what_filled():
    partial = dict(REAL_ORDER, fill_count_fp="1.00",
                   initial_count_fp="3.00", remaining_count_fp="2.00",
                   maker_fill_cost_dollars="0.830000")
    got = KalshiExecutionClient.parse_fill(partial, "DOWN")
    assert got["count"] == 1.0, "the two still resting are not a position"


def test_nothing_parses_from_nothing():
    assert KalshiExecutionClient.parse_fill(None, "DOWN") is None
    assert KalshiExecutionClient.parse_fill({}, "DOWN") is None


# ------------------------------------- the legs, reconciled from fills

def build(tmp_path, *, add_state="RECOVERY ADD CANCELLED", add_fills=True):
    store = Store(str(tmp_path / "p.db"))
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side,"
        " entry_limit, take_profit, count, expires_at, close_ms, status,"
        " created_at, entry_order_id, fill_price, fee_paid, exit_price,"
        " exit_count)"
        " VALUES ('base','primary',?,?,'DOWN',0.86,0.99,2,0,0,'exited',0,"
        "'01a0cfde-71e8-7900-96bb-bc903c415d91',0.85,0.0179,0.997,2)",
        (W, TICKER),
    )
    store.db.execute(
        "INSERT INTO recovery_adds (client_order_id, window_open_ms, ticker,"
        " side, state, order_id, base_fill, limit_price, count, placed_ms,"
        " cancelled_ms, cancel_reason, created_ms, updated_ms)"
        " VALUES ('coid',?,?,'DOWN',?,"
        "'01a0cfde-79b8-7184-a6f6-b326acb11db1',0.85,0.83,1,0,"
        "?,'order not found (already filled, expired or cancelled)',0,0)",
        (W, TICKER, add_state, 1 if add_state.endswith("CANCELLED") else None),
    )
    if add_fills:
        store.db.execute(
            "INSERT INTO fills (fill_id, ticker, order_id, action, side, count,"
            " yes_price, no_price, fee_cost, is_taker, filled_ms, window_ms,"
            " synced_at) VALUES ('f1',?, "
            "'01a0cfde-79b8-7184-a6f6-b326acb11db1','sell','no',1.0,"
            "0.17,0.83,0.0,0,1790194034885,?,0)",
            (TICKER, W),
        )
    store.db.commit()
    return store


def test_a_fill_recorded_as_cancelled_is_repaired_from_the_fills(tmp_path):
    store = build(tmp_path)
    assert store.reconcile_recovery_adds(W) == 1
    row = store._dicts("SELECT * FROM recovery_adds")[0]
    assert row["filled_count"] == 1.0
    assert row["fill_price"] == 0.83
    assert "reconciled from broker fills" in row["cancel_reason"]


def test_reconciliation_never_invents_a_fill(tmp_path):
    """No broker fill, no repair. The local record stays as it is."""
    store = build(tmp_path, add_fills=False)
    assert store.reconcile_recovery_adds(W) == 0
    assert (store._dicts("SELECT * FROM recovery_adds")[0]["filled_count"]
            or 0) == 0


def test_reconciliation_is_idempotent(tmp_path):
    store = build(tmp_path)
    assert store.reconcile_recovery_adds(W) == 1
    assert store.reconcile_recovery_adds(W) == 0, "a repaired row is not redone"


def test_the_position_holds_both_legs_apart(tmp_path):
    store = build(tmp_path)
    pos = store.position_for_window(W)
    kinds = {leg["kind"]: leg for leg in pos["legs"]}
    assert kinds["base"]["count"] == 2.0 and kinds["base"]["price"] == 0.85
    assert kinds["recovery"]["count"] == 1.0
    assert kinds["recovery"]["price"] == 0.83
    assert pos["contracts"] == 3.0


def test_the_total_cost_matches_the_broker(tmp_path):
    """Kalshi's settlement record says `no_total_cost_dollars: 2.530000`."""
    store = build(tmp_path)
    pos = store.position_for_window(W)
    assert pos["cost"] == 2.53
    assert round(pos["total_cost"], 4) == 2.5479


def test_a_pending_add_contributes_nothing(tmp_path):
    store = build(tmp_path, add_state="RECOVERY ADD PENDING", add_fills=False)
    pos = store.position_for_window(W)
    assert pos["contracts"] == 2.0
    assert pos["cost"] == 1.70
    add = next(leg for leg in pos["legs"] if leg["kind"] == "recovery")
    assert add["state"] == "pending"


# ------------------------------------------------- and the recap says so

def recap(pos, **over):
    args = dict(side="DOWN", ticker=TICKER, winner="DOWN", won=True,
                traded=True, pnl=0.4456, contracts=pos["contracts"],
                paid=0.85, fee=pos["fees"], exited_at=0.997,
                called_side="DOWN", qualified=True, position=pos,
                snapshot=None)
    args.update(over)
    return plain(messages.result_message(**args))


def test_the_recap_shows_both_legs(tmp_path):
    text = recap(build(tmp_path).position_for_window(W))
    assert "Base: 2 @ 85¢" in text
    assert "Recovery add: 1 @ 83¢" in text


def test_the_recap_states_a_cost_the_profit_can_come_from(tmp_path):
    """$1.72 against a +$0.45 profit on a sale at 99.7c is arithmetically
    impossible; $2.55 over three contracts is not."""
    text = recap(build(tmp_path).position_for_window(W))
    assert "Total cost $2.55 for 3 contracts" in text
    assert "$1.72" not in text


def test_the_recap_says_how_much_of_the_position_was_sold(tmp_path):
    text = recap(build(tmp_path).position_for_window(W))
    assert "2 of 3" in text
    assert "1 ran to settlement" in text


def test_a_partial_exit_is_not_counted_at_the_sale(tmp_path):
    """Part of this money arrived at expiry, hours after the sale."""
    text = recap(build(tmp_path).position_for_window(W))
    assert "the rest settled at expiry" in text
    assert "Already counted at the sale" not in text


def test_a_full_exit_is_counted_at_the_sale(tmp_path):
    store = build(tmp_path, add_fills=False, add_state="RECOVERY ADD PENDING")
    pos = store.position_for_window(W)
    text = recap(pos)
    assert "Already counted at the sale" in text


def test_a_pending_add_is_named_rather_than_dropped(tmp_path):
    """A reader who sees nothing cannot tell an add that never happened from
    one the message forgot - which is exactly what went wrong."""
    store = build(tmp_path, add_state="RECOVERY ADD PENDING", add_fills=False)
    text = recap(store.position_for_window(W))
    assert "Recovery add: pending" in text


def test_the_profit_is_the_brokers_and_is_labelled_as_combined(tmp_path):
    text = recap(build(tmp_path).position_for_window(W))
    assert "Combined realised Profit $0.45" in text
    assert "all legs" in text


# ------------------------------------- the archive never feeds learning

def test_the_observation_archive_is_not_a_learning_source():
    """The archive held Binance-derived columns until 2026-09-23. It is not a
    corpus source, so those rows cannot reach a Kalshi-native fit - and this
    pins that it stays that way."""
    import inspect

    from btc15_signal import learning, learning_data

    for module in (learning_data, learning):
        src = inspect.getsource(module)
        assert "FROM observations" not in src
        assert "observations WHERE" not in src


def test_rows_keyed_on_the_old_contract_are_excluded_not_rekeyed():
    """`brti-1` rows carry no raw features, so they cannot be expressed under
    `brti-2` and are counted out explicitly rather than silently dropped."""
    from btc15_signal import learning_data

    assert learning_data._setup_key({"ask": 0.8}) is None
    assert learning_data._setup_key(
        {"ask": 0.8, "brti_normalized_distance": 12.0}) is None
    assert learning_data._setup_key({
        "ask": 0.8, "brti_normalized_distance": 12.0,
        "brti_aligned_momentum_bps": 7.0,
    }) is not None
