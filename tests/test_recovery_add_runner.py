"""The add-on against a fake broker: races, restarts and the cumulative cap.

`test_recovery_add.py` pins the decision, which is pure. This pins the part
that cannot be reasoned about - what happens when a cancel loses to a fill, a
response is lost, a restart lands mid-flight, or the same fill is reported
twice. Each of those, done wrong, is either a duplicate contract or a budget
that quietly resets.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.brti import BRTIFeatures  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.recovery_add import AddState, client_order_id  # noqa: E402
from btc15_signal.recovery_add_runner import RecoveryAddRunner  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

TICKER = "KXBTC15M-26SEP221330-30"
WINDOW = 1_790_000_000_000
NOW = int(time.time() * 1000)


class FakeContract:
    ticker = TICKER
    close_ms = WINDOW + 900_000

    def ask(self, side):
        return 0.75


class FakeTrader:
    """A broker that can be told to lose a race."""

    def __init__(self, *, fill_on_status=False, cancel_ok=True, resting=0.0):
        self.placed = []
        self.cancelled = []
        self.fill_on_status = fill_on_status
        self.cancel_ok = cancel_ok
        self._resting = resting

    async def place_resting_buy(self, **kwargs):
        # Kalshi rejects a duplicate client_order_id; so does this.
        if any(p["client_order_id"] == kwargs["client_order_id"] for p in self.placed):
            raise RuntimeError("duplicate client_order_id")
        self.placed.append(kwargs)
        return {"order_id": f"ord-{len(self.placed)}"}

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return (True, "cancelled") if self.cancel_ok else (False, "order not found")

    async def order_status(self, order_id):
        if self.fill_on_status:
            return {
                "order_id": order_id, "status": "executed",
                "maker_fill_count": 1, "taker_fill_count": 0,
                "average_fill_price_dollars": 0.79, "fees_paid_dollars": 0.0,
            }
        return {"order_id": order_id, "status": "resting"}

    async def resting_exposure(self):
        return (1, self._resting)


def features(side="DOWN", distance=15.0, momentum=-20.0, stale=False):
    signed = -50.0 if side == "DOWN" else 50.0
    return BRTIFeatures(
        event_ticker="E", ts_ms=NOW, target=86_430.49,
        value=86_430.49 * (1 + signed / 10_000), signed_distance_bps=signed,
        brti_momentum_bps=momentum, brti_volatility_bps=3.0,
        brti_normalized_distance=distance, samples=300, span_ms=300_000,
        stale=stale, settlement_projection=86_400.0,
    )


def make(tmp_path, **overrides):
    values = {"recovery_add_enabled": True, "database_path": str(tmp_path / "s.db")}
    values.update(overrides)
    settings = Settings(**values)
    store = Store(str(tmp_path / "s.db"))
    # A realised loss, so recovery is active.
    store.record_realised(
        "KXBTC15M-PRIOR", NOW - (NOW % 86_400_000) + 3_600_000, -0.80, False,
        "exchange", NOW,
    )
    # An open base position on the window under test.
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side, "
        "entry_limit, take_profit, count, expires_at, close_ms, status, "
        "created_at, fill_price) VALUES "
        "('p1','primary',?,?,'DOWN',0.77,0,1,?,?,'filled',?,0.77)",
        (WINDOW, TICKER, NOW + 60_000, WINDOW + 900_000, NOW),
    )
    store.db.commit()
    return settings, store


def run(runner, trader, crossed=False, remaining=500, feats=None):
    asyncio.run(runner.step(
        trader=trader, contract=FakeContract(), features=feats or features(),
        crossed=crossed, remaining_s=remaining, now_ms=NOW, opened=WINDOW,
    ))


# --------------------------------------------------------- no duplicates

def test_two_passes_place_exactly_one_order(tmp_path):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    trader = FakeTrader()
    run(runner, trader)
    run(runner, trader)
    assert len(trader.placed) == 1


def test_a_restart_does_not_place_a_second_order(tmp_path):
    """A fresh runner and a fresh Store, same position - the id is the same."""
    settings, store = make(tmp_path)
    run(RecoveryAddRunner(settings, store), FakeTrader())

    reopened = Store(str(tmp_path / "s.db"))
    trader = FakeTrader()
    run(RecoveryAddRunner(settings, reopened), trader)
    assert trader.placed == []


def test_the_order_uses_the_deterministic_id(tmp_path):
    settings, store = make(tmp_path)
    trader = FakeTrader()
    run(RecoveryAddRunner(settings, store), trader)
    assert trader.placed[0]["client_order_id"] == client_order_id(
        TICKER, "DOWN", WINDOW
    )


def test_the_limit_is_two_cents_below_the_recorded_fill(tmp_path):
    settings, store = make(tmp_path)
    trader = FakeTrader()
    run(RecoveryAddRunner(settings, store), trader)
    assert trader.placed[0]["price"] == 0.75   # 0.77 fill - 0.02


# ------------------------------------------------------------- the races

def test_a_cancel_that_loses_to_a_fill_records_the_fill(tmp_path):
    """The dangerous one. Cancel is refused because the order already filled;
    recording a cancellation would lose a contract we now hold."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    run(runner, FakeTrader())

    racing = FakeTrader(cancel_ok=False, fill_on_status=True)
    # Conditions have failed, so the runner will try to cancel.
    run(runner, racing, feats=features(side="UP"))

    row = store.open_add(WINDOW)
    assert row["state"] == AddState.EXECUTED
    assert row["filled_count"] == 1
    assert row["fill_price"] == 0.79


def test_a_fill_reported_twice_charges_the_budget_once(tmp_path):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    run(runner, FakeTrader())
    filling = FakeTrader(fill_on_status=True)
    run(runner, filling)
    first, _ = store.add_budget_committed()
    run(runner, filling)
    second, fills = store.add_budget_committed()
    assert first == second
    assert fills == 1


def test_a_restart_reconciles_a_pending_order_that_filled(tmp_path):
    settings, store = make(tmp_path)
    run(RecoveryAddRunner(settings, store), FakeTrader())

    reopened = Store(str(tmp_path / "s.db"))
    asyncio.run(
        RecoveryAddRunner(settings, reopened).reconcile(
            FakeTrader(fill_on_status=True), NOW
        )
    )
    assert reopened.open_add(WINDOW)["state"] == AddState.EXECUTED


# -------------------------------------------------- the cumulative budget

def test_the_budget_is_cumulative_and_survives_a_restart(tmp_path):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    run(runner, FakeTrader())
    run(runner, FakeTrader(fill_on_status=True))
    committed, fills = store.add_budget_committed()
    assert committed > 0 and fills == 1

    reopened = Store(str(tmp_path / "s.db"))
    assert reopened.add_budget_committed() == (committed, fills)


def test_the_budget_does_not_reset_on_a_loss(tmp_path):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    run(runner, FakeTrader())
    run(runner, FakeTrader(fill_on_status=True))
    before, _ = store.add_budget_committed()

    store.record_realised(
        "KXBTC15M-ANOTHER", NOW - (NOW % 86_400_000) + 7_200_000, -2.5, False,
        "exchange", NOW,
    )
    store.recovery_state(4)
    assert store.add_budget_committed()[0] == before


def test_room_counts_lifetime_fills_and_resting_orders(tmp_path):
    _settings, store = make(tmp_path)
    assert store.add_budget_room(30.0, 0.0) == 30.0
    assert store.add_budget_room(30.0, 5.0) == 25.0
    store._bump_add_budget(4.0, NOW)
    store.db.commit()
    assert store.add_budget_room(30.0, 5.0) == 21.0


def test_an_exhausted_budget_blocks_the_add(tmp_path):
    settings, store = make(tmp_path)
    store._bump_add_budget(30.0, NOW)
    store.db.commit()
    trader = FakeTrader()
    run(RecoveryAddRunner(settings, store), trader)
    assert trader.placed == []
    assert store.open_add(WINDOW)["state"] == AddState.SKIPPED


def test_unreadable_exposure_blocks_the_add(tmp_path):
    settings, store = make(tmp_path)
    trader = FakeTrader(resting=-1.0)
    run(RecoveryAddRunner(settings, store), trader)
    assert trader.placed == []
    assert "unknown" in (store.open_add(WINDOW)["cancel_reason"] or "")


# --------------------------------------------------------------- shadow

def test_shadow_mode_records_the_decision_and_places_nothing(tmp_path):
    settings, store = make(tmp_path, recovery_add_enabled=False)
    trader = FakeTrader()
    run(RecoveryAddRunner(settings, store), trader)
    row = store.open_add(WINDOW)
    assert trader.placed == []
    assert row is not None
    assert row["state"] == AddState.SKIPPED
    assert "shadow" in row["cancel_reason"]
    assert row["limit_price"] == 0.75, "it still records the price it would rest at"


def test_unknown_crossing_history_is_not_a_pass(tmp_path):
    settings, store = make(tmp_path)
    trader = FakeTrader()
    asyncio.run(RecoveryAddRunner(settings, store).step(
        trader=trader, contract=FakeContract(), features=features(),
        crossed=None, remaining_s=500, now_ms=NOW, opened=WINDOW,
    ))
    assert trader.placed == []
    assert "crossing history" in (store.open_add(WINDOW)["cancel_reason"] or "")


def test_the_runner_never_raises_when_the_broker_is_broken(tmp_path):
    settings, store = make(tmp_path)

    class Broken:
        async def resting_exposure(self):
            raise RuntimeError("down")

        async def place_resting_buy(self, **_):
            raise RuntimeError("down")

        async def order_status(self, _):
            raise RuntimeError("down")

        async def cancel_order(self, _):
            raise RuntimeError("down")

    run(RecoveryAddRunner(settings, store), Broken())  # must not raise
