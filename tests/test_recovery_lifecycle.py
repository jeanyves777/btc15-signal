"""The add-on end to end: place, fill, cancel, race, restart, and reach zero.

The first live deployment proved only the REFUSAL path - one add skipped. The
operator was right that this does not demonstrate placement, filling,
cancellation or recovery accounting. These are the paths that had never been
exercised, each one asserted on the state the system actually persists.

Also pinned here, because both were live defects:

  * "crossed since entry" must be measured from the FILL. The first live
    evaluation looked back to the window open and vetoed an add on a crossing
    that happened 4m42s before the position existed.
  * the fold must replay in REALISATION order with a stable tie-breaker, so a
    duplicate sync or a restart cannot produce a different deficit.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.brti import BRTIFeatures  # noqa: E402
from btc15_signal.capital import ny_day_start_ms
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.recovery_add import AddState  # noqa: E402
from btc15_signal.recovery_add_runner import RecoveryAddRunner  # noqa: E402
from btc15_signal.store import Store  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

TICKER = "KXBTC15M-26SEP221500-00"
NOW = int(time.time() * 1000)
DAY = 86_400_000
TODAY = ny_day_start_ms(NOW)
WINDOW = TODAY + 3_600_000


class FakeContract:
    ticker = TICKER
    close_ms = WINDOW + 900_000

    def ask(self, side):
        return 0.75


class Broker:
    def __init__(self, **kw):
        self.placed, self.cancelled = [], []
        self.fill = kw.get("fill")            # dict merged into order status
        self.cancel_ok = kw.get("cancel_ok", True)
        self.resting = kw.get("resting", 0.0)
        self.status = kw.get("status", "resting")

    async def place_resting_buy(self, **kwargs):
        if any(p["client_order_id"] == kwargs["client_order_id"] for p in self.placed):
            raise RuntimeError("duplicate client_order_id")
        self.placed.append(kwargs)
        return {"order_id": "ord-1"}

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return (True, "cancelled") if self.cancel_ok else (False, "order not found")

    async def order_status(self, order_id):
        base = {"order_id": order_id, "status": self.status}
        return {**base, **self.fill} if self.fill else base

    async def balance_dollars(self):
        return getattr(self, 'balance', 30.0)

    async def resting_exposure(self):
        return (len(self.placed), self.resting)


def features(side="UP", distance=15.0, momentum=20.0, stale=False):
    signed = 50.0 if side == "UP" else -50.0
    return BRTIFeatures(
        event_ticker="E", ts_ms=NOW, target=86_430.49,
        value=86_430.49 * (1 + signed / 10_000), signed_distance_bps=signed,
        brti_momentum_bps=momentum, brti_volatility_bps=3.0,
        brti_normalized_distance=distance, samples=300, span_ms=300_000,
        stale=stale, settlement_projection=86_400.0,
    )


def build(tmp_path, *, deficit_loss=-0.80, fill_price=0.72):
    settings = Settings(
        recovery_add_enabled=True, database_path=str(tmp_path / "s.db")
    )
    store = Store(str(tmp_path / "s.db"))
    store.set_recovery_epoch(TODAY, NOW)
    store.record_realised(
        "KXBTC15M-PRIOR", TODAY + 600_000, deficit_loss, False, "exchange", NOW
    )
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side, "
        "entry_limit, take_profit, count, expires_at, close_ms, status, "
        "created_at, fill_price) VALUES "
        "('p1','primary',?,?,'UP',0.74,0,1,?,?,'filled',?,?)",
        (WINDOW, TICKER, NOW + 60_000, WINDOW + 900_000, NOW, fill_price),
    )
    store.db.execute(
        "INSERT INTO fills (fill_id, ticker, order_id, action, side, count, "
        "yes_price, no_price, fee_cost, is_taker, filled_ms, window_ms, synced_at) "
        "VALUES ('f1',?,'o1','buy','yes',1,?,?,0.01,1,?,?,?)",
        (TICKER, fill_price, 1 - fill_price, NOW, WINDOW, NOW),
    )
    store.db.commit()
    return settings, store


def step(runner, broker, *, crossed=False, remaining=500, feats=None):
    asyncio.run(runner.step(
        trader=broker, contract=FakeContract(), features=feats or features(),
        crossed=crossed, remaining_s=remaining, now_ms=NOW, opened=WINDOW,
    ))


# ------------------------------------------------------- placement -> fill

def test_placement_then_full_fill_is_recorded_with_fee_and_maker_flag(tmp_path):
    settings, store = build(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = Broker()
    step(runner, broker)
    assert len(broker.placed) == 1
    assert broker.placed[0]["price"] == 0.70          # 0.72 fill - 2c
    assert store.open_add(WINDOW)["state"] == AddState.PENDING

    broker.fill = {
        "status": "executed",
        "fill_count_fp": "1.0",
        "initial_count_fp": "1.0",
        "remaining_count_fp": "0.0",
        "maker_fill_cost_dollars": "0.7",
        "taker_fill_cost_dollars": "0.0",
        "maker_fees_dollars": "0.0",
        "taker_fees_dollars": "0.0",
        "no_price_dollars": "0.7",
        "yes_price_dollars": "0.3",
        "outcome_side": "no",
    }
    step(runner, broker)
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.EXECUTED
    assert row["filled_count"] == 1
    assert row["fill_price"] == 0.70
    assert row["is_taker"] == 0, "a resting order that filled is a maker"
    assert row["conditions_at_fill"], "conditions at the fill are recorded"


def test_a_partial_fill_is_banked_for_the_amount_filled(tmp_path):
    settings, store = build(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = Broker()
    step(runner, broker)
    broker.fill = {
        "status": "resting",
        "fill_count_fp": "0.4",
        "initial_count_fp": "1.0",
        "remaining_count_fp": "0.6",
        "maker_fill_cost_dollars": "0.28",
        "taker_fill_cost_dollars": "0.0",
        "maker_fees_dollars": "0.004",
        "taker_fees_dollars": "0.0",
        "no_price_dollars": "0.7",
        "yes_price_dollars": "0.3",
        "outcome_side": "no",
    }
    step(runner, broker)
    row = store.open_add(WINDOW)
    assert row["filled_count"] == 0.4
    spend, fills = store.add_budget_committed()
    assert spend == round(0.4 * 0.70 + 0.004, 6)
    assert fills == 1


def test_a_clean_cancel_records_the_reason(tmp_path):
    settings, store = build(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = Broker()
    step(runner, broker)
    step(runner, broker, feats=features(side="DOWN"))   # direction flipped
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.CANCELLED
    assert "flipped" in row["cancel_reason"]
    assert broker.cancelled == ["ord-1"]
    assert store.add_budget_committed() == (0.0, 0), "a cancel spends nothing"


def test_cancel_racing_a_fill_banks_the_fill_not_the_cancel(tmp_path):
    settings, store = build(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = Broker()
    step(runner, broker)
    broker.cancel_ok = False
    broker.fill = {
        "status": "executed",
        "fill_count_fp": "1.0",
        "initial_count_fp": "1.0",
        "remaining_count_fp": "0.0",
        "maker_fill_cost_dollars": "0.7",
        "taker_fill_cost_dollars": "0.0",
        "maker_fees_dollars": "0.0",
        "taker_fees_dollars": "0.0",
        "no_price_dollars": "0.7",
        "yes_price_dollars": "0.3",
        "outcome_side": "no",
    }
    step(runner, broker, feats=features(side="DOWN"))
    assert store.open_add(WINDOW)["state"] == AddState.EXECUTED


def test_restart_reconciles_a_cancelled_order(tmp_path):
    settings, store = build(tmp_path)
    step(RecoveryAddRunner(settings, store), Broker())
    reopened = Store(str(tmp_path / "s.db"))
    asyncio.run(
        RecoveryAddRunner(settings, reopened).reconcile(
            Broker(status="canceled"), NOW
        )
    )
    row = reopened.open_add(WINDOW)
    assert row["state"] == AddState.CANCELLED
    assert "reconciled after restart" in row["cancel_reason"]


# ------------------------------------------- recovery accounting to zero

def test_recovery_reaches_zero_and_the_next_trade_is_base(tmp_path):
    settings, store = build(tmp_path, deficit_loss=-0.50)
    assert store.recovery_state(4).active

    store.record_realised(
        "KXBTC15M-WIN", TODAY + 1_200_000, 0.50, True, "exchange", NOW + 1_000
    )
    state = store.recovery_state(4)
    assert state.deficit == 0.0
    assert not state.active

    from btc15_signal.main import recovery_size
    assert recovery_size(store, settings, 1, 0.75) == (1, "")


def test_an_unreachable_requirement_skips_the_add_it_does_not_upsize(tmp_path):
    """The failure the epoch bug would have caused: a requirement no position
    can meet must refuse the add, never reach for more size."""
    settings, store = build(tmp_path, deficit_loss=-40.0)
    required = store.recovery_state(4).required_per_trade()
    assert required > 1.0, "precondition: unreachable"

    broker = Broker()
    step(RecoveryAddRunner(settings, store), broker)
    assert broker.placed == []
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.SKIPPED
    assert "recovery needs" in row["cancel_reason"]
    assert row["count"] == settings.recovery_add_max_contracts == 1


def test_add_pnl_is_reported_separately_from_the_account(tmp_path):
    settings, store = build(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = Broker()
    step(runner, broker)
    broker.fill = {
        "status": "executed",
        "fill_count_fp": "1.0",
        "initial_count_fp": "1.0",
        "remaining_count_fp": "0.0",
        "maker_fill_cost_dollars": "0.7",
        "taker_fill_cost_dollars": "0.0",
        "maker_fees_dollars": "0.0",
        "taker_fees_dollars": "0.0",
        "no_price_dollars": "0.7",
        "yes_price_dollars": "0.3",
        "outcome_side": "no",
    }
    step(runner, broker)
    store.update_add(
        store.open_add(WINDOW)["client_order_id"],
        {"realised_pnl": 0.30, "settled": 1, "updated_ms": NOW},
    )
    summary = store.add_pnl_summary()
    assert summary["filled"] == 1
    assert summary["pnl"] == 0.30


# ---------------------------------------------- the three separate limits

def test_the_budget_state_keeps_the_account_and_the_history_apart(tmp_path):
    _settings, store = build(tmp_path)
    store._bump_add_budget(12.0, NOW)
    store.db.commit()
    state = store.add_budget_state(30.0, resting=3.0, balance=25.0)
    assert state["account_size"] == 30.0
    assert state["broker_balance"] == 25.0
    assert state["open_and_resting_exposure"] == 3.0
    assert state["account_room"] == 25.0, "min(cash, ceiling - exposure)"
    assert state["lifetime_spend"] == 12.0, "recorded, but it gates nothing"
    assert state["realised_add_pnl"] == 0.0, "spend is not a loss figure"


def test_room_is_the_lesser_of_cash_and_the_ceiling(tmp_path):
    _settings, store = build(tmp_path)
    assert store.account_room(30.0, balance=5.0, exposure=0.0) == 5.0
    assert store.account_room(30.0, balance=100.0, exposure=28.0) == 2.0
    assert store.account_room(30.0, balance=-1.0, exposure=0.0) == -1.0
    assert store.account_room(30.0, balance=10.0, exposure=-1.0) == -1.0


def test_profitable_adds_do_not_exhaust_the_account(tmp_path):
    """The correction. Three profitable $9 adds spend $27 of LIFETIME total,
    but the money settled back - the account still has room."""
    _settings, store = build(tmp_path)
    for _ in range(3):
        store._bump_add_budget(9.0, NOW)
    store.db.commit()
    assert store.add_budget_room(30.0, 0.0) == 3.0, "the old lifetime view"
    assert store.account_room(30.0, balance=31.0, exposure=0.0) == 30.0


# --------------------------------------------- order-sensitive rebuilding

def test_the_deficit_is_identical_after_a_duplicate_sync(tmp_path):
    _settings, store = build(tmp_path)
    first = store.recovery_state(4).deficit
    for _ in range(4):
        store.sync_ledger_from_settlements(NOW)
        assert store.recovery_state(4).deficit == first


def test_the_deficit_survives_a_restart_unchanged(tmp_path):
    _settings, store = build(tmp_path)
    store.record_realised(
        "KXBTC15M-W", TODAY + 1_200_000, 0.20, True, "exchange", NOW + 1_000
    )
    before = store.recovery_state(4).deficit
    assert Store(str(tmp_path / "s.db")).recovery_state(4).deficit == before


def test_an_early_cash_out_reduces_the_deficit_in_realisation_order(tmp_path):
    """A cash-out realises while its own window is still running, so it can
    land before a market that opened earlier. Ordering by the market's clock
    would apply the profit to a debt that did not exist yet."""
    _settings, store = build(tmp_path, deficit_loss=-1.00)
    # Realised LATER, but on an EARLIER window.
    store.record_realised(
        "KXBTC15M-EARLYWINDOW", TODAY + 300_000, 0.40, True, "cash_out",
        NOW + 5_000,
    )
    state = store.recovery_state(4)
    assert state.deficit == 0.60
    assert Store(str(tmp_path / "s.db")).recovery_state(4).deficit == 0.60


# ------------------------------------------------ crossing from the fill

def test_entry_time_comes_from_the_broker_fill_not_the_window(tmp_path):
    _settings, store = build(tmp_path)
    entry = store.position_entry_ms(WINDOW, TICKER)
    assert entry == NOW, "the fill time, not the window open"
    assert entry > WINDOW


def test_entry_time_is_none_when_nothing_is_known(tmp_path):
    _settings, store = build(tmp_path)
    assert store.position_entry_ms(WINDOW, "KXBTC15M-NOT-OURS") is None


def test_max_net_profit_matches_the_operators_arithmetic():
    from btc15_signal.recovery_add import max_net_profit
    assert round(max_net_profit(2, 0.75, fee), 4) == 0.4737
    assert round(max_net_profit(2, 0.90, fee), 4) == 0.1874
