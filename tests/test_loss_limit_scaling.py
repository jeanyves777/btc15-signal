"""The daily loss floor scales with the base size (operator, 2026-09-27).

"Loss limit must scale" - with the base, which scales with capital uncapped
(FINDINGS 106). The configured floor is the floor for 2 base contracts, the
base it was set for, and it moves in proportion, so it allows the same number
of losses per day whatever the size.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import btc15_signal.main as main  # noqa: E402
from btc15_signal import autotrade  # noqa: E402
from btc15_signal.capital import Capital, ny_day  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

NOW = int(time.time() * 1000)


def store_at(tmp_path, base):
    store = Store(str(tmp_path / "s.db"))
    if base is not None:
        store.record_capital_day(Capital(
            ny_day=ny_day(NOW), reconciled_cash=30.0 * base, open_exposure=0.0,
            base_contracts=base, account_ceiling=30.0, reconciled_ms=NOW - 1))
    return store


def test_at_the_reference_base_the_floor_is_as_configured(tmp_path):
    s = Settings(auto_daily_loss_limit=5.0)
    assert main.scaled_loss_limit(store_at(tmp_path, 2), s, NOW) == 5.0


def test_at_base_4_every_instances_floor_doubles(tmp_path):
    store = store_at(tmp_path, 4)
    for configured, expected in ((5.0, 10.0), (7.0, 14.0), (10.0, 20.0), (20.0, 40.0)):
        s = Settings(auto_daily_loss_limit=configured)
        assert main.scaled_loss_limit(store, s, NOW) == expected


def test_before_the_days_review_the_floor_is_tighter_never_looser(tmp_path):
    s = Settings(auto_daily_loss_limit=10.0)
    assert main.scaled_loss_limit(store_at(tmp_path, None), s, NOW) == 5.0


def test_a_telegram_override_is_scaled_the_same_way(tmp_path):
    store = store_at(tmp_path, 4)
    store.set_setting("auto_daily_loss_limit", 6.0, NOW)
    assert main.scaled_loss_limit(store, Settings(), NOW) == 12.0


def test_the_scaling_can_be_turned_off(tmp_path):
    store = store_at(tmp_path, 4)
    off = Settings(auto_daily_loss_limit=5.0, loss_limit_scales_with_base=False)
    assert main.scaled_loss_limit(store, off, NOW) == 5.0
    unsized = Settings(auto_daily_loss_limit=5.0, capital_sizing_enabled=False)
    assert main.scaled_loss_limit(store, unsized, NOW) == 5.0


def test_the_order_path_blocks_on_the_scaled_floor(tmp_path):
    """-$7 of realised loss at base 4: the old fixed $5 floor would have
    stopped the instrument; the scaled $10 floor does not. At -$10 it does."""
    s = Settings(auto_daily_loss_limit=5.0, auto_trade_enabled=True)
    limits = main.auto_limits(store_at(tmp_path, 4), s, NOW)
    assert limits.daily_loss_limit == 10.0

    def state(realised):
        return autotrade.AutoState(trades_today=0, trades_last_hour=0,
                                   seconds_since_last=1e9, realised_today=realised,
                                   open_positions=0)

    assert "daily loss limit" not in autotrade.auto_block_reason(
        limits, state(-7.0), 0.80, True)
    assert "daily loss limit reached" in autotrade.auto_block_reason(
        limits, state(-10.0), 0.80, True)


def test_the_shipped_settings():
    s = Settings()
    assert s.loss_limit_scales_with_base is True
    assert s.loss_limit_reference_base == 2
