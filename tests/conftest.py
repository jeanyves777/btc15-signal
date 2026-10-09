"""Reset module-level surface state between tests.

`surface` carries two pieces of process-global state, both set once at service
startup and read by every message: `_INSTRUMENT` (which instrument this process
is) and `_INTELLIGENCE` (whether the learned layer can act, which `compose`
renders as a warning line when it cannot).

That is correct for a service and wrong for a test suite. A test that sets an
unhealthy intelligence state and does not clear it leaves every later
`compose` carrying an extra line - which is how `test_the_fixed_order_holds`
came to fail only when run after the intelligence tests, and to pass in
isolation. An order-dependent failure is worse than a plain one: it points at
the wrong test.

Resetting here rather than in each test means a new test cannot reintroduce the
leak by forgetting a `finally`.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# RECOVERY IS OFF IN SERVICE since 2026-09-28 (config.recovery_enabled=False;
# FINDINGS 108), but its mechanics are still tested - the suite opts back in,
# and tests/test_live_instruments.py pins the OFF default and what it does.
os.environ.setdefault("RECOVERY_ENABLED", "true")
# Likewise the combo recovery (off in the live .env since 09-29): its
# mechanics stay tested whatever the operator's .env says.
os.environ.setdefault("RECOVERY_COMBO_ENABLED", "true")
# And the early cash-out (OFF in the live .env since 2026-10-05, operator: "disable
# cash out for now"): its mechanics stay tested whatever the operator's .env says.
os.environ.setdefault("CASH_OUT_ENABLED", "true")
# The chase (ON in the live .env since 2026-10-05): OFF for the suite, so the 60 s
# retry stays tested as it was; tests/test_allsignal_chase.py switches it on itself.
os.environ.setdefault("ALLSIGNAL_CHASE_MAX", "0")
# Historical cushion/skip tests opt out; price-confirmation tests opt in explicitly.
os.environ.setdefault("ALLSIGNAL_SKIP_WAIT_MIN_ASK", "0")
os.environ.setdefault("ALLSIGNAL_SKIP_WAIT_MAX_ASK", "0")
# The after-a-loss stakes and the mirrors trading on past the primary (ON in the live
# .env since 2026-10-05, FINDINGS 163): OFF for the suite, so the stake rules stay
# tested as they were; tests/test_new_setup_1005.py switches them on itself.
for _key, _off in (("ALLSIGNAL_AFTER_LOSS_STAKE", "0"), ("MIRROR_ALLSIGNAL_AFTER_LOSS_ADD", "0"),
                   ("MIRROR_1_ALLSIGNAL_AFTER_LOSS_STAKE", "0"),
                   ("MIRROR_2_ALLSIGNAL_AFTER_LOSS_STAKE", "0"),
                   ("MIRROR_3_ALLSIGNAL_AFTER_LOSS_STAKE", "0"),
                   ("MIRROR_1_ALLSIGNAL_AFTER_TARGET_STAKE", "0"),
                   ("MIRROR_2_ALLSIGNAL_AFTER_TARGET_STAKE", "0"),
                   ("MIRROR_3_ALLSIGNAL_AFTER_TARGET_STAKE", "0"),
                   ("MIRROR_AFTER_PRIMARY_DONE", "false")):
    os.environ.setdefault(_key, _off)


@pytest.fixture(autouse=True)
def _clean_surface_state():
    from btc15_signal import surface

    before_instrument = getattr(surface, "_INSTRUMENT", "")
    before_recovery = getattr(surface, "_RECOVERY", True)
    before_combo = getattr(surface, "_RECOVERY_COMBO", True)
    before_intelligence = dict(getattr(surface, "_INTELLIGENCE", {}) or {})
    yield
    surface._INSTRUMENT = before_instrument
    surface._RECOVERY = before_recovery
    surface._RECOVERY_COMBO = before_combo
    surface.set_intelligence_state(before_intelligence or None)
