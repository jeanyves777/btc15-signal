"""Recovery must be visible. It ran for an hour and nobody could see it.

On 2026-09-22 a -$0.86 loss armed recovery, the add-on was blocked twice by
margins of fractions - momentum -0.6 bps against a floor of zero, distance 9.9x
against 10x - one real order was placed at $0.82, and the deficit cleared. None
of it reached Telegram: the word "recovery" did not appear in any message
builder. The operator asked "I did not see the recovery happening", which was
the correct question about a subsystem moving real money in silence.

These tests pin that it is now visible, and - just as important - that it stays
quiet when there is nothing to say.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import messages  # noqa: E402
from btc15_signal.capital import ny_day_start_ms  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

NOW = int(time.time() * 1000)
START = ny_day_start_ms(NOW)


def lose(store, amount=-0.86, ticker="KX-LOSS", offset=9):
    ms = START + offset * 3_600_000
    store.record_realised(
        ticker, ms, amount, False, "exchange", NOW, realised_ms=NOW + offset
    )


def win(store, amount=0.90, ticker="KX-WIN", offset=10):
    ms = START + offset * 3_600_000
    store.record_realised(
        ticker, ms, amount, True, "exchange", NOW, realised_ms=NOW + offset
    )


def blocked_add(store, reason="momentum not aligned (-0.6 bps)"):
    store.db.execute(
        "INSERT INTO recovery_adds (client_order_id, window_open_ms, ticker, "
        "side, state, cancel_reason, limit_price, count, created_ms, updated_ms) "
        "VALUES ('c1',?,'KXBTC15M-X','UP','RECOVERY ADD SKIPPED',?,0.82,1,?,?)",
        (START, reason, NOW, NOW),
    )
    store.db.commit()


# ------------------------------------------------------------ transitions

def test_a_loss_announces_that_recovery_armed(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    lose(store)
    event, state = store.recovery_transition(NOW)
    assert event == "armed"
    text = messages.recovery_armed(state, "KX-LOSS settled -0.86")
    assert "RECOVERY ARMED" in text
    assert "0.86" in text
    assert "one contract" in text, "it must say the base size is unchanged"


def test_clearing_announces_once_and_then_goes_quiet(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    lose(store)
    assert store.recovery_transition(NOW)[0] == "armed"
    assert store.recovery_transition(NOW)[0] is None, "armed is announced once"

    win(store)
    event, _ = store.recovery_transition(NOW + 5_000)
    assert event == "cleared"
    assert store.recovery_transition(NOW + 6_000)[0] is None


def test_a_restart_does_not_re_announce(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    lose(store)
    assert store.recovery_transition(NOW)[0] == "armed"
    reopened = Store(str(tmp_path / "s.db"))
    assert reopened.recovery_transition(NOW + 1_000)[0] is None


def test_a_second_loss_during_recovery_is_not_a_new_arming(tmp_path):
    """The deficit grows; recovery was already on. Announcing again would
    read as a second, separate event."""
    store = Store(str(tmp_path / "s.db"))
    lose(store)
    store.recovery_transition(NOW)
    lose(store, -0.50, "KX-LOSS-2", offset=11)
    assert store.recovery_transition(NOW + 1_000)[0] is None


# ----------------------------------------------------------- the live line

def test_the_money_block_shows_the_deficit_while_active(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    lose(store)
    store.recovery_state(4)          # fold, as the service loop does
    text = messages._money_block(store.money_snapshot(NOW))
    assert "Recovery" in text
    assert "0.86" in text
    assert "a trade" in text, "the per-trade share is the actionable number"


def test_the_line_says_WHY_no_add_was_made(tmp_path):
    """The gap that sent the operator to the database."""
    store = Store(str(tmp_path / "s.db"))
    lose(store)
    store.recovery_state(4)
    blocked_add(store)
    text = messages._money_block(store.money_snapshot(NOW))
    assert "no add" in text
    assert "momentum not aligned" in text


def test_a_resting_add_is_shown_with_its_broker_order(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    lose(store)
    store.recovery_state(4)
    store.db.execute(
        "INSERT INTO recovery_adds (client_order_id, window_open_ms, ticker, "
        "side, state, order_id, limit_price, count, created_ms, updated_ms) "
        "VALUES ('c2',?,'KXBTC15M-X','UP','RECOVERY ADD PENDING',"
        "'01a0cb5c-ba58-768e-957c-5c2be36fc6ac',0.82,1,?,?)",
        (START, NOW, NOW),
    )
    store.db.commit()
    text = messages._money_block(store.money_snapshot(NOW))
    assert "resting at 0.82" in text
    assert "01a0cb5c" in text, "the broker order id, so it can be looked up"


def test_nothing_is_shown_when_recovery_is_inactive(tmp_path):
    """A quiet system stays quiet. A permanent status line for a subsystem
    that is off is noise, and noise is what makes the real line invisible."""
    store = Store(str(tmp_path / "s.db"))
    win(store)
    store.recovery_state(4)
    text = messages._money_block(store.money_snapshot(NOW))
    assert "Recovery" not in text


def test_the_cleared_message_carries_the_money(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    lose(store)
    win(store)
    store.recovery_state(4)
    text = messages.recovery_cleared(store.money_snapshot(NOW))
    assert "RECOVERY CLEARED" in text
    assert "returns to base" in text
    assert "Today (New York)" in text


def test_the_named_trigger_is_the_actual_last_loss(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    win(store, 0.30, "KX-EARLIER", offset=5)
    lose(store, -0.44, "KX-THE-ONE", offset=12)
    assert store.last_realised_loss() == ("KX-THE-ONE", -0.44)
