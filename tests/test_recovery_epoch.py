"""Backfilled history must not retroactively open a recovery debt.

This is a bug that shipped and was caught on the running system. Widening the
settlement lookback - correct in itself, so a 23:45 window settling after
midnight is not lost - pulled three days of finished markets into the ledger
with `recovery_applied` unset. The fold replayed all of them on top of the live
figure and drove the deficit from $0.85 to $10.90.

Nothing looked broken: recovery still reported ACTIVE. But the requirement is
deficit/steps, so it became $2.73 a trade, which no two-contract position can
ever reach. The add-on would have skipped every setup for a reason that read
like arithmetic rather than a fault - a gate silently jammed shut.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.capital import ny_day_start_ms
from btc15_signal.store import Store  # noqa: E402

DAY = 86_400_000
NOW = int(time.time() * 1000)
TODAY = ny_day_start_ms(NOW)


def settlement(store: Store, ticker: str, window_ms: int, pnl: float) -> None:
    store.db.execute(
        "INSERT OR REPLACE INTO settlements (ticker, market_result, pnl, "
        "settled_ms, synced_at, window_ms) VALUES (?,?,?,?,?,?)",
        (ticker, "yes" if pnl > 0 else "no", pnl, window_ms + 900_000, NOW, window_ms),
    )
    store.db.commit()


def test_history_older_than_the_epoch_arrives_already_applied(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.set_recovery_epoch(TODAY, NOW)

    settlement(store, "OLD-LOSS", TODAY - 2 * DAY, -5.0)
    settlement(store, "OLD-LOSS-2", TODAY - DAY, -3.0)
    store.sync_ledger_from_settlements(NOW)

    state = store.recovery_state(4)
    assert state.deficit == 0.0, "history must not open a debt"
    assert not state.active


def test_todays_loss_still_opens_a_debt_after_the_guard(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.set_recovery_epoch(TODAY, NOW)

    settlement(store, "OLD-LOSS", TODAY - 2 * DAY, -5.0)
    settlement(store, "TODAY-LOSS", TODAY + 3_600_000, -0.80)
    store.sync_ledger_from_settlements(NOW)

    state = store.recovery_state(4)
    assert state.deficit == 0.80
    assert state.active


def test_a_midnight_settlement_still_reaches_the_ledger(tmp_path):
    """The reason the lookback was widened in the first place: a 23:45 window
    carries a previous-day `window_ms` and settles after midnight."""
    store = Store(str(tmp_path / "s.db"))
    store.set_recovery_epoch(TODAY - DAY, NOW)

    late = TODAY - 900_000  # yesterday 23:45
    settlement(store, "MIDNIGHT", late, -0.90)
    store.sync_ledger_from_settlements(NOW)

    assert store.db.execute(
        "SELECT COUNT(*) FROM daily_ledger WHERE ticker = 'MIDNIGHT'"
    ).fetchone()[0] == 1
    assert store.recovery_state(4).deficit == 0.90


def test_without_an_epoch_nothing_is_pre_applied(tmp_path):
    """Epoch 0 means the guard is inert - a fresh database folds what it finds
    rather than silently discarding it."""
    store = Store(str(tmp_path / "s.db"))
    assert store.recovery_epoch_ms() == 0
    settlement(store, "A-LOSS", TODAY + 3_600_000, -0.50)
    store.sync_ledger_from_settlements(NOW)
    assert store.recovery_state(4).deficit == 0.50


def test_resync_is_idempotent_and_moves_nothing(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.set_recovery_epoch(TODAY, NOW)
    settlement(store, "TODAY-LOSS", TODAY + 3_600_000, -0.80)
    store.sync_ledger_from_settlements(NOW)
    first = store.recovery_state(4).deficit

    for _ in range(3):
        store.sync_ledger_from_settlements(NOW)
    assert store.recovery_state(4).deficit == first


def test_the_requirement_stays_reachable_for_two_contracts(tmp_path):
    """The symptom that made the bug visible: a deficit built from replayed
    history demands more per trade than any two-contract position can make."""
    store = Store(str(tmp_path / "s.db"))
    store.set_recovery_epoch(TODAY, NOW)
    settlement(store, "TODAY-LOSS", TODAY + 3_600_000, -1.6416)
    store.sync_ledger_from_settlements(NOW)

    required = store.recovery_state(4).required_per_trade()
    # The best a 2-contract position can net is under a dollar; the whole
    # point of the plan divisor is that the share stays inside that.
    assert required < 0.95, f"unreachable requirement: {required}"
