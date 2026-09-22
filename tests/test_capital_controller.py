"""The single sizing authority, the New York day, and funds that cannot be
spent twice.

Also the two implementation details the operator asked to see proved rather
than asserted:

  * replaying a broker fill or settlement UPDATES the same event; it never
    creates a second one
  * broker cash is already net of resting reservations, so exposure must not
    be deducted from it a second time

The partial-exit arithmetic is pinned here too: entry cost and fees are
allocated to the portion actually sold, and the settlement accounts only for
what was still held.
"""

import asyncio
import datetime as dt
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.capital import (  # noqa: E402
    CapitalController,
    ny_day,
    ny_day_start_ms,
    tier_for,
)
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

NY = ZoneInfo("America/New_York")
NOW = int(time.time() * 1000)


class Broker:
    def __init__(self, cash=30.0, resting=0.0):
        self.cash, self.resting = cash, resting

    async def balance_dollars(self):
        return self.cash

    async def resting_exposure(self):
        return (0, self.resting)


def ms(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso).timestamp() * 1000)


# --------------------------------------------------- the New York day

def test_the_day_boundary_is_new_york_not_utc():
    """In July, New York is UTC-4, so midnight local is 04:00 UTC. Anything
    between 00:00 and 04:00 UTC still belongs to the PREVIOUS New York day -
    four hours in which a UTC-keyed book would file trades under a day the
    exchange has not started yet."""
    assert ny_day(ms("2026-07-15T03:30:00+00:00")) == "2026-07-14"
    assert ny_day(ms("2026-07-15T04:30:00+00:00")) == "2026-07-15"
    # In January it is UTC-5, so the same 04:30 UTC is still the day before.
    assert ny_day(ms("2026-01-15T04:30:00+00:00")) == "2026-01-14"


def test_the_boundary_moves_with_daylight_saving():
    """EDT is UTC-4, EST is UTC-5, so midnight New York lands at a different
    UTC hour depending on the season. A fixed offset is wrong for half the
    year."""
    summer = ny_day_start_ms(ms("2026-07-15T12:00:00+00:00"))
    winter = ny_day_start_ms(ms("2026-01-15T12:00:00+00:00"))
    assert dt.datetime.fromtimestamp(summer / 1000, dt.UTC).hour == 4   # EDT
    assert dt.datetime.fromtimestamp(winter / 1000, dt.UTC).hour == 5   # EST


def test_the_spring_forward_day_is_twenty_three_hours():
    start = ny_day_start_ms(ms("2026-03-08T12:00:00+00:00"))
    nxt = ny_day_start_ms(ms("2026-03-09T12:00:00+00:00"))
    assert (nxt - start) == 23 * 3_600_000


def test_the_fall_back_day_is_twenty_five_hours():
    start = ny_day_start_ms(ms("2026-11-01T12:00:00+00:00"))
    nxt = ny_day_start_ms(ms("2026-11-02T12:00:00+00:00"))
    assert (nxt - start) == 25 * 3_600_000


def test_a_day_start_is_midnight_local_every_time():
    for iso in ("2026-01-15T12:00:00+00:00", "2026-07-15T12:00:00+00:00",
                "2026-03-08T12:00:00+00:00", "2026-11-01T12:00:00+00:00"):
        start = ny_day_start_ms(ms(iso))
        local = dt.datetime.fromtimestamp(start / 1000, dt.UTC).astimezone(NY)
        assert (local.hour, local.minute) == (0, 0)


# ------------------------------------------------------------- the tier

def test_the_tier_is_a_step_function_of_reconciled_capital():
    assert tier_for(29.99, 30.0, 2) == 1
    assert tier_for(30.0, 30.0, 2) == 1
    assert tier_for(60.0, 30.0, 2) == 2
    assert tier_for(6_000.0, 30.0, 2) == 2, "the ceiling binds"
    assert tier_for(0.0, 30.0, 2) == 1, "never zero - that is a silent stop"


def test_the_tier_only_changes_at_a_review(tmp_path):
    settings = Settings(database_path=str(tmp_path / "s.db"))
    store = Store(str(tmp_path / "s.db"))
    controller = CapitalController(settings, store)

    asyncio.run(controller.reconcile(Broker(cash=30.0), NOW))
    assert controller.base_contracts(NOW) == 1

    # The account doubles intraday. The tier must NOT follow until a review.
    assert controller.base_contracts(NOW) == 1
    asyncio.run(controller.reconcile(Broker(cash=75.0), NOW))
    assert controller.base_contracts(NOW) == 1, "same day, already reviewed"

    asyncio.run(controller.reconcile(Broker(cash=75.0), NOW, force=True))
    assert controller.base_contracts(NOW) == 2


def test_unrealised_gains_do_not_raise_the_tier(tmp_path):
    """Settled cash only. An open position marked at +$40 is not capital."""
    settings = Settings(database_path=str(tmp_path / "s.db"))
    store = Store(str(tmp_path / "s.db"))
    store.set_setting("open_mark", 40.0, NOW)
    controller = CapitalController(settings, store)
    asyncio.run(controller.reconcile(Broker(cash=30.0), NOW))
    assert controller.base_contracts(NOW) == 1


def test_an_unreadable_balance_keeps_yesterdays_tier(tmp_path):
    settings = Settings(database_path=str(tmp_path / "s.db"))
    store = Store(str(tmp_path / "s.db"))
    controller = CapitalController(settings, store)
    asyncio.run(controller.reconcile(Broker(cash=60.0), NOW))
    assert controller.base_contracts(NOW) == 2
    asyncio.run(controller.reconcile(Broker(cash=-1.0), NOW, force=True))
    assert controller.base_contracts(NOW) == 2, "unknown is not zero"


def test_reconcile_never_raises(tmp_path):
    settings = Settings(database_path=str(tmp_path / "s.db"))
    store = Store(str(tmp_path / "s.db"))

    class Broken:
        async def balance_dollars(self):
            raise RuntimeError("down")

        async def resting_exposure(self):
            raise RuntimeError("down")

    assert asyncio.run(CapitalController(settings, store).reconcile(Broken(), NOW)) is None


# ------------------------------------------------------------- the funds

def test_broker_cash_is_not_reduced_by_resting_twice(tmp_path):
    """Kalshi's `balance` is the AVAILABLE balance and resting orders already
    reserve against it. Deducting exposure from the cash as well would refuse
    orders the account can afford."""
    settings = Settings(
        database_path=str(tmp_path / "s.db"), recovery_add_test_budget=1000.0
    )
    store = Store(str(tmp_path / "s.db"))
    controller = CapitalController(settings, store)
    room = asyncio.run(controller.available(Broker(cash=20.0, resting=5.0), NOW))
    assert room == 20.0, "cash is already net of the resting reservation"


def test_the_account_ceiling_is_a_separate_constraint(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "s.db"), recovery_add_test_budget=30.0
    )
    store = Store(str(tmp_path / "s.db"))
    controller = CapitalController(settings, store)
    # Plenty of cash, but the test is only authorised to be $30 big.
    room = asyncio.run(controller.available(Broker(cash=500.0, resting=28.0), NOW))
    assert room == 2.0


def test_unknown_cash_or_exposure_yields_no_room(tmp_path):
    settings = Settings(database_path=str(tmp_path / "s.db"))
    store = Store(str(tmp_path / "s.db"))
    controller = CapitalController(settings, store)
    assert asyncio.run(controller.available(Broker(cash=-1.0), NOW)) == -1.0
    assert asyncio.run(controller.available(Broker(resting=-1.0), NOW)) == -1.0
    assert asyncio.run(controller.available(None, NOW)) == -1.0


def test_two_orders_cannot_reserve_the_same_money(tmp_path):
    settings = Settings(database_path=str(tmp_path / "s.db"))
    store = Store(str(tmp_path / "s.db"))
    controller = CapitalController(settings, store)
    assert controller.reserve("base:W1", 0.80, NOW) is True
    assert controller.reserve("base:W1", 0.80, NOW) is False, "same key, once"
    assert controller.reserve("add:W1", 0.75, NOW) is True


def test_a_reservation_reduces_available_funds(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "s.db"), recovery_add_test_budget=1000.0
    )
    store = Store(str(tmp_path / "s.db"))
    controller = CapitalController(settings, store)
    before = asyncio.run(controller.available(Broker(cash=10.0), NOW))
    controller.reserve("base:W1", 4.0, NOW)
    after = asyncio.run(controller.available(Broker(cash=10.0), NOW))
    assert before - after == 4.0
    controller.release("base:W1")
    assert asyncio.run(controller.available(Broker(cash=10.0), NOW)) == before


def test_a_reservation_does_not_expire_on_a_clock(tmp_path):
    """A timer cannot decide an order is gone. An order may still be resting,
    or its submission outcome unknown; releasing its money because five
    minutes passed hands the same dollars out twice. Only a proven outcome -
    cancelled, rejected, expired, or filled and now counted as position
    exposure - releases a reservation."""
    store = Store(str(tmp_path / "s.db"))
    store.reserve_funds("still-resting", 5.0, NOW - (60 * 60_000))
    assert store.reserved_funds(NOW) == 5.0, "an hour old and still held"
    store.release_funds("still-resting", reason="broker confirmed cancelled")
    assert store.reserved_funds(NOW) == 0.0


def test_two_different_orders_cannot_overspend_the_same_balance(tmp_path):
    """A UNIQUE key stops the SAME order reserving twice. It does nothing
    about two DIFFERENT orders against one balance, which is the case that
    actually overspends."""
    store = Store(str(tmp_path / "s.db"))
    assert store.reserve_funds("base:W1", 6.0, NOW, available=10.0) is True
    assert store.reserve_funds("add:W1", 6.0, NOW, available=10.0) is False, (
        "6 + 6 does not fit in 10"
    )
    assert store.reserved_funds(NOW) == 6.0
    assert store.reserve_funds("add:W1", 4.0, NOW, available=10.0) is True
    assert store.reserved_funds(NOW) == 10.0


# --------------------------------------------- event identity and partials

def test_replaying_a_settlement_updates_the_same_event(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    for _ in range(4):
        store.record_realised(
            "KX-A", NOW, -0.80, False, "exchange", NOW, realised_ms=NOW
        )
    rows = store.settlement_events("KX-A")
    assert len(rows) == 1, "a replay must never create a second event"
    assert rows[0]["amount"] == -0.80


def test_a_revised_settlement_updates_the_amount_in_place(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.record_realised("KX-A", NOW, 0.55, True, "cash_out", NOW, realised_ms=NOW)
    store.record_realised("KX-A", NOW, 0.54, True, "exchange", NOW, realised_ms=NOW)
    rows = store.settlement_events("KX-A")
    ids = {r["event_id"] for r in rows}
    # The exit is keyed on its own instant, so two partial exits are two
    # events; the settlement keeps one stable identity per market.
    assert ids == {f"cash_out:KX-A:{NOW}", "exchange:KX-A"}
    total = sum(r["amount"] for r in rows)
    assert round(total, 6) == 0.54, "the events sum to the exchange's figure"


def test_a_partial_exit_and_its_settlement_are_two_events(tmp_path):
    """The exit realises at its fill; the settlement accounts only for what
    was still held, as the remainder."""
    store = Store(str(tmp_path / "s.db"))
    exit_ms, settle_ms = NOW, NOW + 180_000
    store.record_realised(
        "KX-P", NOW, 0.20, True, "cash_out", NOW, realised_ms=exit_ms
    )
    store.record_realised(
        "KX-P", NOW, 0.50, True, "exchange", NOW, realised_ms=settle_ms
    )
    rows = store.settlement_events("KX-P")
    assert len(rows) == 2
    assert rows[0]["realised_ms"] == exit_ms
    assert rows[1]["realised_ms"] == settle_ms
    assert round(rows[1]["amount"], 6) == 0.30, "the remainder, not the total"
    assert round(sum(r["amount"] for r in rows), 6) == 0.50


def test_partial_exit_allocates_entry_cost_and_fees_to_the_portion_sold():
    """Selling one of two contracts carries one contract's cost and half the
    entry fee. Charging the whole entry fee to the part sold overstates it and
    understates what the remaining contract still owes."""
    from btc15_signal.main import partial_exit_pnl

    banked = partial_exit_pnl(
        paid=0.70, bid=0.90, filled=1, held=2, entry_fee=0.04, exit_fee=0.01
    )
    assert round(banked, 6) == round((0.90 - 0.70) * 1 - 0.02 - 0.01, 6)

    whole = partial_exit_pnl(
        paid=0.70, bid=0.90, filled=2, held=2, entry_fee=0.04, exit_fee=0.02
    )
    assert round(whole, 6) == round((0.90 - 0.70) * 2 - 0.04 - 0.02, 6)
