"""THE ALL-SIGNAL STRATEGY IS LOCKED (operator, 2026-09-28 16:3x ET).

"From now that strategy is locked in - freeze everything, except when I tell
you to change base size."

These pin what is running so that no edit - a refactor, a "small fix", a new
gate - can change it silently. A failure here means the strategy was touched:
revert it, or get the operator's explicit say-so and update the pin in the same
change, with the decision recorded in FINDINGS 111. The ONE thing the operator
reserved is the base size (`allsignal_stake`, and her `allsignal_budget`), and
it changes only on their instruction.
"""

import hashlib
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import main  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.mirror import MirrorTarget  # noqa: E402

# Re-pinned 2026-09-29: operator explicitly requested BTC-only execution,
# a persistent 3% daily profit pause, fresh capital NOW, and Telegram updates.
# The new branch reports a profit-paused entry separately from a missed fill.
# Re-pinned 2026-09-30 on the operator's word ("make primary account base size 6
# and apply $3 after the 8% target hit only to mine the primary"; FINDINGS 117):
# spawn_allsignal sizes by allsignal_stake_now and records the stake on the row.
# The hash now covers main.allsignal_stake_now too - the function that sets the
# live $ size (review 2026-09-30: the call-site pin alone let the rule change).
# Re-pinned 2026-09-30 17:5x on the operator's word (losses back below the
# target "invalidate the daily target hit and trade size back to the $6";
# FINDINGS 120): allsignal_stake_now follows the day's P&L against the target.
# Re-pinned 2026-10-05 on the operator's word ("Let implement I $30 boost only below
# 8%"; "Mirrors: boost all three by $1 // and stop for the day at 8% ... Affoue ... $6
# base and $8 after 1 loss and the after target hit $3"; FINDINGS 163): the primary's
# stake after a loss, a copied signal booked once the primary is done for the day, and
# the mirrors' own stakes - the two new sizing functions are pinned with it.
# Re-pinned again 2026-10-05 (review wf_83445e67-f9f): a copied window is said on
# Telegram; a copy is booked only when a mirror bought it (FINDINGS 163).
LOCKED_CODE = "4ebc15e61e95cf07"


def test_the_locked_settings():
    d = Settings.model_fields
    assert d["allsignal_instruments"].default == "BTC,GOLD"
    assert d["allsignal_mirror"].default is True, "both accounts"
    # THE BASE SIZE - the operator's to change, and only theirs.
    assert d["allsignal_stake"].default == 1.00
    assert MirrorTarget.__dataclass_fields__["allsignal_budget"].default == 1.0


def test_the_locked_order_path():
    src = "".join(inspect.getsource(f) for f in (
        main.allsignal_on, main.spawn_allsignal, main._allsignal_order,
        main.main_strategy_on, main.allsignal_stake_now,
        main.allsignal_after_loss_boost, main.mirror_stake_now))
    assert hashlib.sha256(src.encode()).hexdigest()[:16] == LOCKED_CODE, (
        "the all-signal strategy's code changed - it is LOCKED (FINDINGS 111)")


# THE CASH-OUT IS PART OF IT (operator, 2026-09-28 18:1x: "cash out must be
# part of the system at all levels ... it cashes out at max profit, no need to
# wait for expiry"). The main strategy's rule, on the $1 book; pinned like the
# rest. Evidence beside the decision in FINDINGS 111.
# Re-pinned 2026-09-28 19:4x on the operator's word ("close these loops"):
# the sale price is read back with retries and confirmed from broker fills.
# 2026-09-29: operator requested $5 primary/$2 mirrors. Cash-out mechanics
# are unchanged; its Telegram call now receives the configured entry budget.
# Re-pinned 2026-09-30 (same instruction): the cash-out notice names the stake
# its trade was sized at ($6, or $3 after the target). Mechanics unchanged.
CASH_OUT_CODE = "43327444ca10f7ce"


def test_the_locked_cash_out():
    d = Settings.model_fields
    assert d["cash_out_enabled"].default is True
    assert d["cash_out_capture"].default == 0.90
    assert d["cash_out_min_bid"].default == 0.90
    assert d["cash_out_at_bid"].default == 0.98
    assert d["exit_slippage"].default == 0.01
    assert d["min_exit_price"].default == 0.90
    assert d["exit_min_seconds"].default == 60
    src = inspect.getsource(main.allsignal_cash_out)
    assert hashlib.sha256(src.encode()).hexdigest()[:16] == CASH_OUT_CODE, (
        "the all-signal cash-out changed - it is LOCKED (FINDINGS 111)")
    loop = " ".join(inspect.getsource(main.service).split())
    assert "await allsignal_cash_out(" in loop, "and it runs every poll"


# ADOPTED 2026-09-29 on the operator's word ("adopt 5 and ship it live"): after a
# losing trade the next signal waits for a 5 bps cushion (FINDINGS 112).
def test_the_after_loss_cushion_is_pinned():
    d = Settings.model_fields
    assert d["allsignal_after_loss_cushion_bps"].default == 5.0
    assert d["allsignal_cushion_min_left_s"].default == 120
    loop = " ".join(inspect.getsource(main.service).split())
    assert "allsignal_cushion_poll(store, settings, trader, contract, snapshot," in loop
    path = " ".join(inspect.getsource(main.primary_signal).split())
    assert "allsignal_on_alert(store, settings, trader, contract, prediction.side," in path


# THE CUSHION RULE'S OWN CODE is pinned too (safety review, 2026-09-30: the
# call-site strings alone passed through a rewrite). Adopted 09-29 with the
# review fixes; re-pin only with the operator's say-so and a FINDINGS note.
# Re-pinned 2026-10-02 on the operator's word ("the 15 minutes after 2 losses is the
# one I want live"; FINDINGS 142): after two losses, never against the 15-min trend.
# (Known losses only, and the skip's write guarded - review 2026-10-02.)
# Re-pinned 2026-10-05 (FINDINGS 163, same instruction): the cushion and the trend skip
# read the day's TAKEN sequence - copied signals included once the primary is done -
# through _taken_rows/_taken_lost, pinned with them. Filled rows read exactly as before.
# Re-pinned 2026-10-07 (operator: "set the better candidate"): allsignal_on_alert also
# skips while a chop range is locked (allsignal_ohlc_lock_skip, off unless set).
CUSHION_CODE = "db93f187e99a5c66"


def test_the_cushion_rule_code_is_pinned():
    fns = (main.cushion_bps, main.allsignal_after_loss, main.allsignal_on_alert,
           main.allsignal_cushion_poll, main._skip_wait, main._start_wait,
           main._end_wait, main._save_wait,
           main.allsignal_loss_streak, main.brti_trend_bps, main.allsignal_trend_skip,
           main._taken_rows, main._taken_lost)
    src = "".join(inspect.getsource(f) for f in fns)
    assert hashlib.sha256(src.encode()).hexdigest()[:16] == CUSHION_CODE, (
        "the after-loss cushion code changed - it is pinned (FINDINGS 112)")


# THE RETRY (operator, 2026-09-30: "retry 60 after check is everything still
# aligned"): pinned like the rest; re-pin only on the operator's word.
# Re-pinned 2026-09-30 with FINDINGS 117 ("$3 after the 8% target"): a retry
# after the primary's target goes at the lower stake, never at its $6 claim.
# Re-pinned 2026-10-05 on the operator's word ("better to take it than just letting
# it go"; FINDINGS 155): a miss is chased at the moved price, up to 93c.
# Re-pinned 2026-10-05 (FINDINGS 163): the retry sizes for its own window (the boost).
# Re-pinned again 2026-10-05 (review): pre-funding covers the after-a-loss stakes.
RETRY_CODE = "3f63a5e96564e44c"


def test_the_retry_code_is_pinned():
    src = inspect.getsource(main.allsignal_retry_poll) + inspect.getsource(main.prewarm_order_path)
    assert hashlib.sha256(src.encode()).hexdigest()[:16] == RETRY_CODE, (
        "the retry code changed - it is pinned (FINDINGS 112)")
    assert main.RETRY_AFTER_MS == 60_000
