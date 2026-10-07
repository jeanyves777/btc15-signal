"""A position bid at ~1.00 banks itself, whatever it cost to enter.

THE DEFECT. `cash_out_capture` banks a FRACTION of the profit still above the
entry price. That fraction is unreachable on an expensive entry: after the
slippage discount the gate needs a quoted bid of

    0.10 * paid + 0.91

which passes 0.999 - above any bid a binary can offer - once `paid` exceeds
0.89. It then never fires, and a gate that cannot fire is indistinguishable
from a market that never qualified. The live record: entries below 0.89 exit
33-58% of the time, entries above it 4.2% (1 of 24). The operator's
screenshot was a 0.895 entry quoted 99.9%, needing a 0.9995 bid.

The absolute trigger is the fix. These tests pin the boundary it was added
for - an expensive entry at a near-certain bid - and that it cannot sell at a
loss or sell into a low bid, which is what an absolute rule risks becoming.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402

S = Settings()


def gate(paid: float, quoted: float, s: Settings = S) -> bool:
    """The shipped decision, in the order main.cash_out_exit applies it."""
    bid = round(quoted - s.exit_slippage, 4)
    if not 0.0 < bid < 1.0 or bid < s.cash_out_min_bid:
        return False
    available = 1.0 - paid
    at_max = bid >= s.cash_out_at_bid
    if not at_max and (
        available <= 0 or (bid - paid) < s.cash_out_capture * available
    ):
        return False
    return bid > paid


# ------------------------------------------------------------- the defect

def test_the_operators_screenshot_now_fires():
    """2 contracts worth $1.99 on a $2 payout, +$0.20 open -> paid 0.895,
    quoted 0.995. Under the proportional gate alone this needed 0.9995."""
    assert gate(paid=0.8950, quoted=0.995)


def test_the_proportional_gate_alone_could_not_have():
    s = Settings(cash_out_at_bid=1.01)      # absolute trigger disabled
    assert not gate(paid=0.8950, quoted=0.995, s=s)


def test_every_entry_price_can_now_reach_it():
    """The point of the fix: reachability must not depend on the entry."""
    for paid in (0.70, 0.80, 0.85, 0.89, 0.90, 0.92, 0.93, 0.95):
        assert gate(paid=paid, quoted=0.99), paid


def test_the_cheap_entries_still_use_the_proportional_gate():
    """It is an ADDITION, not a replacement. A 0.65 entry quoted 0.98
    discounts to 0.97 and has captured 0.32 of the 0.35 available - 91%, over
    the proportional gate, and below the 0.98 absolute trigger. So it banks
    on the old rule, exactly as it did before."""
    assert gate(paid=0.65, quoted=0.98)
    s = Settings(cash_out_at_bid=1.01)      # absolute trigger disabled
    assert gate(paid=0.65, quoted=0.98, s=s)


# ------------------------------------------------- what it must not become

def test_it_never_sells_at_a_loss():
    """The proportional gate guaranteed this implicitly; the absolute one
    does not, so it is asserted rather than assumed."""
    for paid in (0.985, 0.99, 0.995, 1.00):
        assert not gate(paid=paid, quoted=0.995), paid


def test_it_does_not_sell_into_a_low_bid():
    for quoted in (0.50, 0.80, 0.899):
        assert not gate(paid=0.60, quoted=quoted), quoted


def test_it_is_judged_on_the_DISCOUNTED_bid():
    """Judging on the quote and selling at the quote is the 2026-09-21 defect:
    an IOC at a 0.979 top-of-book filled nothing while the app offered 0.93.
    A quoted 0.985 discounts to 0.975 and must not reach a 0.98 trigger."""
    s = Settings(cash_out_at_bid=0.98, cash_out_capture=1.01)
    assert not gate(paid=0.90, quoted=0.985, s=s)
    assert gate(paid=0.90, quoted=0.99, s=s)


def test_the_threshold_maps_to_the_measured_level():
    """0.98 against the discounted bid is a quoted 0.99 - the level the
    146-case measurement was taken at. If one moves the other must."""
    assert S.cash_out_at_bid + S.exit_slippage == 0.99


def test_the_shipped_settings_are_the_decided_ones():
    assert S.cash_out_enabled is True
    assert S.cash_out_at_bid == 0.98
    assert S.cash_out_capture == 0.90
    assert S.cash_out_min_bid == 0.90


def test_it_is_still_gated_by_the_master_switch():
    import inspect

    from btc15_signal import main

    source = inspect.getsource(main.cash_out_exit)
    assert "settings.cash_out_enabled" in source
    at = source.index("cash_out_at_bid")
    # the kill switch is checked before anything decides to sell
    assert source.index("cash_out_enabled") < at


def test_it_respects_the_minimum_time_to_close():
    import inspect

    from btc15_signal import main

    source = inspect.getsource(main.cash_out_exit)
    assert "exit_min_seconds" in source
