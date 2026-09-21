"""THE PERMANENT RULE.

Time-of-day may increase or reduce intelligence confidence. It can never stop
the 15-minute trading system.

Specifically it must NOT:
  * skip a 15-minute market
  * stop polling
  * prevent the normal strategy from evaluating
  * block an otherwise qualified order
  * disable alerts or archiving

and it must NOT touch position size, which belongs to the operator alone. That
last one was violated once, on 2026-09-21, when "weight" was read as position
size and briefly multiplied into the order budget. A comment is not a test, so
every prohibition above is asserted here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.regime import (  # noqa: E402
    HOURLY,
    MAX_WEIGHT,
    MIN_WEIGHT,
    _baseline_and_tau,
    adjust,
    confidence_points,
    label_for,
    weight_for_hour,
)

MAIN = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")


def auto_block() -> str:
    block = MAIN.split("---- unattended execution")[1]
    return block[: block.index("    head = head_for(store, settings)")]


# --------------------------------------------------- the five prohibitions


def test_it_never_blocks_an_otherwise_qualified_order():
    """The order path sizes from settings and refuses from auto_block_reason.
    Neither may consult the clock."""
    block = auto_block()
    assert "contracts_for_budget(limits.budget, contract_ask)" in block
    refusal = block[: block.index("if blocked:")]
    for forbidden in ("weight_for_hour", "weight_at", "regime_apply", "confidence_points"):
        assert forbidden not in refusal, f"{forbidden} reached the refusal logic"


def test_it_never_touches_position_size():
    from btc15_signal import regime

    assert not hasattr(regime, "apply"), "no helper may scale a budget"
    source = Path("src/btc15_signal/regime.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]
    assert "budget" not in body
    assert "contracts" not in body


def test_it_never_skips_a_market_or_stops_polling():
    """The poll loop and the market lookup must not mention regime at all."""
    loop = MAIN[MAIN.index("async def run("):] if "async def run(" in MAIN else MAIN
    lookup = loop.split("contract = await kalshi.active_market(now_ms)")
    assert len(lookup) > 1, "the market lookup must still exist"
    before = lookup[0]
    assert "weight_for_hour" not in before.split("while True:")[-1]
    assert "continue" not in before.split("weight_at")[-1].split("\n")[0]


def test_it_never_prevents_the_strategy_from_evaluating():
    """`rule.matches` decides qualification; the clock is not one of its inputs."""
    strategy = Path("src/btc15_signal/strategy.py").read_text(encoding="utf-8")
    for forbidden in ("hour", "session", "regime", "weight_for_hour"):
        assert forbidden not in strategy.lower().split("def matches")[1].split("def ")[0]


def test_it_never_disables_alerts_or_archiving():
    """Archiving is unconditional, and regime is written INTO it, not around it."""
    assert '"regime_weight": _regime.weight' in MAIN
    assert '"confidence_adjustment"' in MAIN
    archive = MAIN.split("def archive_observation(")[1].split("\ndef ")[0]
    assert "return" not in archive.split('_regime = weight_for_hour')[1].split("row = {")[0]


# --------------------------------------------------- what it IS allowed to do


def test_confidence_is_stated_as_base_adjustment_adjusted():
    """The operator's format: base HIGH, adjustment -25, adjusted MEDIUM."""
    scored = adjust(4, weight_for_hour(10))
    assert scored["base_label"] == "HIGH"
    assert scored["regime_points"] < 0
    assert scored["adjusted_label"] == "MEDIUM"
    assert scored["adjusted_points"] == scored["base_points"] + scored["regime_points"]


def test_a_favourable_hour_can_raise_confidence_too():
    """'increase OR reduce' - it must be able to move both ways."""
    assert confidence_points(weight_for_hour(14)) > 0
    assert confidence_points(weight_for_hour(10)) < 0


def test_the_adjustment_is_bounded_so_it_cannot_erase_a_verdict():
    for hour in range(24):
        assert abs(confidence_points(weight_for_hour(hour))) <= 25
        assert label_for(adjust(4, weight_for_hour(hour))["adjusted_points"]) in (
            "HIGH", "MEDIUM", "LOW"
        )


# --------------------------------------------------- the estimate itself


def test_no_hour_can_ever_reach_zero_weight():
    assert MIN_WEIGHT > 0
    for hour in range(24):
        assert MIN_WEIGHT <= weight_for_hour(hour).weight <= MAX_WEIGHT


def test_an_hour_that_is_pure_noise_weighs_exactly_one():
    """If hours differ only by sampling error, `tau^2` floors at zero and no
    hour moves. Nothing has to be switched off by hand."""
    flat = {h: (200, 0.006, -0.05, 0.062) for h in range(24)}
    _baseline, tau2 = _baseline_and_tau(flat)
    assert tau2 == 0.0
    for hour in range(24):
        assert weight_for_hour(hour, flat).weight == 1.0
        assert confidence_points(weight_for_hour(hour, flat)) == 0


def test_shrinkage_discards_most_of_each_hours_deviation():
    """The hourly table is mostly noise - the pre-registered 17-20 UTC test came
    out p=0.542."""
    for hour in range(24):
        w = weight_for_hour(hour)
        assert abs(w.shrunk_edge - w.baseline) <= abs(w.raw_edge - w.baseline)
        assert 0.0 <= w.shrink < 0.5


def test_the_operators_afternoon_is_not_singled_out():
    """17-20 UTC measured +0.0010/ct [-0.0272, +0.0305], p=0.542, so the block
    must not come out systematically light."""
    afternoon = [weight_for_hour(h).weight for h in (17, 18, 19, 20)]
    assert 0.92 <= sum(afternoon) / len(afternoon) <= 1.08


def test_the_table_covers_every_hour():
    assert set(HOURLY) == set(range(24))
    for n, _mean, low, high in HOURLY.values():
        assert n > 100
        assert low < high
