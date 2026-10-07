"""A third mirror account (operator, 2026-09-30: "I added MIRROR 3 enable it to
trade same as the other MIRRORs")."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal import main  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.mirror import MIRROR_SLOTS, targets_from_settings  # noqa: E402


def three(**extra):
    values = dict(_env_file=None)
    for i in (1, 2, 3):
        values[f"mirror_{i}_api_key_id"] = f"key{i}"
        values[f"mirror_{i}_private_key_path"] = f"path{i}"
        values[f"mirror_{i}_allsignal_budget"] = 2.0
    values.update(extra)
    return Settings(**values)


def test_a_third_mirror_is_read_like_the_others():
    targets = targets_from_settings(three())
    assert [t.name for t in targets] == ["m1", "m2", "m3"]
    m3 = targets[2]
    assert m3.allsignal_budget == 2.0 and m3.auto_fund is True and m3.recovery_budget == 0.0
    assert MIRROR_SLOTS == (1, 2, 3)


def test_a_half_filled_third_block_is_ignored():
    s = three(mirror_3_private_key_path="")
    assert [t.name for t in targets_from_settings(s)] == ["m1", "m2"]


def test_the_third_mirror_is_named_in_messages():
    assert main.mirror_name(three(), "m3") == "Mirror 3"
    assert main.mirror_name(three(mirror_3_label="Cousin"), "m3") == "Cousin"


def test_the_switch_script_knows_the_third_mirror():
    spec = importlib.util.spec_from_file_location("mirror_switch_t", ROOT / "scripts" / "mirror_switch.py")
    ms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ms)
    assert ms.mirrors(three()) == ["m1", "m2", "m3"]
    assert ms.ALIASES["m3"] == "m3"
