"""No undefined name anywhere in src/ or scripts/.

2026-09-27: lifting the recovery half of sizing out of `primary_signal` left
one later line reading `recovery_reason`, a name that no longer existed there.
That line runs only after an order FILLS, and nothing in the suite drives
`primary_signal` with a trader - so 1,613 tests passed, and ETH and SOL each
crashed with a NameError straight after their next real fill. The watchdog
restarted them; the positions were already on the book.

A static check finds that class of mistake in a second, on paths no test can
reach. ruff's F821 (undefined name), F822 (undefined name in __all__) and F823
(local referenced before assignment).
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_no_undefined_names_in_the_service_or_its_scripts():
    try:
        out = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--select", "F821,F822,F823",
             "--output-format", "concise", "src", "scripts"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        pytest.skip("ruff is not installed")
    if "No module named ruff" in out.stderr:
        pytest.skip("ruff is not installed")
    assert out.returncode == 0, out.stdout + out.stderr
