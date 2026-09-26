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

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def _clean_surface_state():
    from btc15_signal import surface

    before_instrument = getattr(surface, "_INSTRUMENT", "")
    before_intelligence = dict(getattr(surface, "_INTELLIGENCE", {}) or {})
    yield
    surface._INSTRUMENT = before_instrument
    surface.set_intelligence_state(before_intelligence or None)
