"""Gold runs on gold's numbers, and BTC keeps running on BTC's.

WHY GOLD NEEDED ITS OWN EVERYTHING (FINDINGS 73). BTC's deployed rule rejects
99.4% of gold setups, and that rejection was a MEASUREMENT ERROR rather than a
verdict. Gold's per-second reference jitters about seven times more than BRTI
in bps, but the jitter mean-reverts: only 4.0% of it survives to settlement
against BTC's 30.5%. `brti_normalized_distance` divides by that jitter, so
gold's median reads 0.83 where BTC's reads 6.92 - while the two instruments are
equivalent in risk-adjusted terms, 0.74x against 0.78x of the actual 15-minute
move.

Measured on gold's own scale the edge is LARGER than BTC's:

    ask 0.60-0.80 | distance 1.5-6.0bp | 11-7 min left
    n=921 over 445 markets and 35 days, win 78.4% at a 0.691 ask
    residual +0.0925 [+0.0613, +0.1219]
    first half +0.1099, second half +0.0768 - both hold, disjoint
    and it beats NOT gating: +0.0743 [+0.0522, +0.1001]

    Refitted 2026-09-25 as a complete SET under brti-4 (FINDINGS 75). The
    earlier figure quoted here - ask 0.65-0.75, n=527, +0.0987 - came from a
    gate-by-gate fit and is superseded. Magnitude carries the open caveat in
    FINDINGS 75a: a denser replay of the same days reads +0.007, and the corpus
    holds only markets whose reference fetch succeeded.

Two structural differences from BTC, both measured and both load-bearing:

  * the distance test is a BAND, not a floor. Gold's edge is level-maintenance
    - it holds its side 75.4% at 2-5bp against BTC's 66.0% - so a LARGE
    distance means the move already happened, which is BTC's edge and not
    gold's. Above 6bp gold's residual collapses to zero.
  * the price band is gold's own, measured on gold. It is NOT below BTC's any
    more: 0.60-0.80 overlaps BTC's 0.70-0.93 across 0.70-0.80, so the property
    worth pinning is that gold's band is measured rather than inherited, which
    is what `test_the_gold_price_band_is_below_btcs` now checks.

These tests pin both, and pin that neither leaks into BTC or ETH.
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import kalshi_signal  # noqa: E402
from btc15_signal.brti import features_from_series  # noqa: E402
from btc15_signal.kalshi_brti import KalshiBRTIRule  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE = 1_790_000_000_000
LEVEL = 4_290.0


def gold_rule() -> KalshiBRTIRule:
    cfg = {k: v for k, v in
           json.loads((ROOT / "strategy_kalshi_gold.json").read_text())
           .items() if not k.startswith("_")}
    return KalshiBRTIRule(**cfg)


def series_at(gap_bps: float, n: int = 2800):
    """A gold path over the full 45 minutes the level lookback now reads.

    Not a flat line. A constant series has no measurable retrace, and the
    reversal gate correctly refuses what it cannot check - so a flat fixture
    tests the fixture rather than the rule.

    AND NOT 400 SECONDS EITHER. The level lookback is 2,700s, so a 400-point
    fixture showed the rule a window that cannot occur live: a single clean move
    scores one rejection, its own departure, and `rejections >= 2` then refuses
    it. Live, every evaluation has the full 45 minutes behind it, and in gold's
    own corpus 65% of decision points score 8+ rejections while only 4 of 2,910
    score zero. So the path here approaches and leaves the level several times
    before the final rise-and-hold, which is what gold's windows actually look
    like - and what its edge, level-maintenance, is measured on.
    """
    target = LEVEL
    top = target * (1 + gap_bps / 10_000)
    tail = 400                      # the setup itself: rise, then hold
    approach = n - tail             # the 40 minutes before it
    points = []
    for i in range(approach):
        # Four passes at the level, each leaving and coming back - the
        # "tested and held" shape, at gold's own scale.
        phase = (i % (approach // 4)) / (approach // 4)
        value = target * (1 + (gap_bps * 0.8 / 10_000) * math.sin(
            phase * 2 * math.pi))
        points.append((BASE + i * 1000, value))
    for j in range(tail):
        if j < tail // 3:
            value = target + (top - target) * (j / (tail // 3))
        else:
            # Holding, with the sub-bp jitter gold's feed actually carries.
            value = top + (0.02 if j % 2 else -0.02)
        points.append((BASE + (approach + j) * 1000, value))
    return points, target


def feats(gap_bps: float):
    points, target = series_at(gap_bps)
    return features_from_series("E", points, target, points[-1][0])


def distance_fact(rule, f, ask=0.70, remaining=600):
    return next(x for x in rule.check_facts(f, ask, remaining)
                if x["name"] == "BRTI distance")


# ------------------------------------------------- the band, not a floor

def test_a_gap_inside_the_band_passes():
    assert distance_fact(gold_rule(), feats(3.2))["passed"]


def test_a_gap_below_the_band_is_refused():
    fact = distance_fact(gold_rule(), feats(0.8))
    assert not fact["passed"]


def test_a_gap_BEYOND_the_band_is_refused():
    """The part that distinguishes gold from BTC. On BTC more distance is
    always better; on gold it means the move already happened, and 5-10bp
    scores +0.0074 spanning zero against 2-5bp's +0.0715."""
    fact = distance_fact(gold_rule(), feats(12.0))
    assert not fact["passed"]
    assert "already happened" in fact["fail_text"]


def test_the_band_edges_are_the_measured_ones():
    rule = gold_rule()
    assert rule.min_abs_distance_bps == 1.5
    assert rule.max_abs_distance_bps == 6.0


# --------------------------------------------- BTC and ETH are untouched

def test_btc_still_uses_the_normalised_floor():
    """No absolute band configured means the rule is exactly what it was."""
    rule = KalshiBRTIRule()
    assert rule.min_abs_distance_bps is None
    assert rule.max_abs_distance_bps is None
    fact = distance_fact(rule, feats(3.2))
    assert "BRTI vol" in (fact["pass_text"] or fact["fail_text"]), \
        "BTC's distance wording changed, so its gate changed"


def test_a_huge_gap_still_PASSES_on_btc():
    """The opposite of gold: BTC wants distance and has no ceiling."""
    rule = KalshiBRTIRule(min_brti_normalized_distance=0.0,
                          moving_min_brti_normalized_distance=0.0)
    assert distance_fact(rule, feats(50.0))["passed"]


def test_eth_config_did_not_acquire_a_band():
    cfg = json.loads((ROOT / "strategy_kalshi_eth.json").read_text())
    assert "min_abs_distance_bps" not in cfg
    assert "max_abs_distance_bps" not in cfg


# ------------------------------------------------- the shipped gold numbers

def test_the_gold_price_band_is_measured_not_inherited():
    """0.60-0.80 against BTC's 0.70-0.93. If these ever converge by accident,
    gold is being traded on BTC's evidence. The exact band is a measurement and
    moves when re-measured - what must hold is that it is not BTC's."""
    rule = gold_rule()
    btc = KalshiBRTIRule()
    assert (rule.min_ask, rule.max_ask) != (btc.min_ask, btc.max_ask)
    assert rule.max_ask < btc.max_ask
    assert rule.min_ask < btc.min_ask


def test_the_entry_window_is_the_one_that_actually_runs():
    """660-420s, narrowed from 700-400 on 2026-09-25 with NO change in
    behaviour. Entry is gated twice and independently: `settings.entry_from_
    seconds`/`entry_to_seconds` (660/360, which no launcher overrides) is
    checked in main.py BEFORE the rule is consulted, and these two keys are
    checked again inside the rule. The corpus holds decision points at 360..660
    seconds only, so 700-400 selected exactly the minutes 660-420 selects.

    The config previously advertised a span the service could never use, which
    would have become a real defect the moment the Settings window was widened:
    gold would have started trading minutes no corpus point covers. FINDINGS 77."""
    rule = gold_rule()
    assert (rule.entry_from_seconds, rule.entry_to_seconds) == (660, 420)
    # And it must not claim a span outside what the service can act on.
    from btc15_signal.config import Settings
    s = Settings()
    assert rule.entry_from_seconds <= s.entry_from_seconds
    assert rule.entry_to_seconds >= s.entry_to_seconds


def test_the_three_level_gates_carry_golds_own_numbers_not_btcs():
    """They used to be SENTINELS - accel >= -1e9, held >= 0s, rejections >= 0 -
    measured off under the 900s lookback, and still counted as passed checks.
    Under brti-4's 45-minute lookback all three were re-measured on gold and all
    three now hold real values: held 90-240s scores +0.0710 [+0.0364, +0.1055]
    against -0.0028 below 30s, rejections 8+ scores +0.0389 [+0.0132, +0.0649],
    and acceleration is a BAND because gold's -10..0 bucket beats its 0..+10 -
    the reverse of BTC, which wants accel high.

    What must not happen is a value arriving from BTC. So each is checked
    against BTC's, and against being a sentinel again."""
    rule = gold_rule()
    btc = KalshiBRTIRule()
    assert rule.min_brti_accel > -1e6, "accel is a sentinel again"
    assert rule.max_brti_accel is not None, "gold caps accel; BTC does not"
    assert btc.max_brti_accel is None
    assert rule.min_brti_held_s > 0.0
    assert rule.min_brti_rejections > 0
    assert rule.min_brti_held_s != btc.min_brti_held_s
    assert (rule.min_brti_accel, rule.min_brti_rejections) != (
        btc.min_brti_accel, btc.min_brti_rejections)


def test_retrace_is_now_measured_and_still_not_btcs():
    """`brti_retrace` was never stored in any corpus, so every fit scored
    candidate sets as though the gate were absent while live enforced BTC's
    inherited (0.60, require-measurable). The backfill records it now. On gold
    it is unmeasurable on 3.8% of points and the cap sits at the widest value
    clearing the volume floor.

    The momentum FLOOR and alignment stay off because neither is measured on
    gold - the rule being that an unmeasured gate is worse than no gate,
    because it looks deliberate."""
    rule = gold_rule()
    btc = KalshiBRTIRule()
    assert rule.max_brti_retrace < 1.0, "retrace is a sentinel again"
    assert (rule.max_brti_retrace, rule.require_measurable_retrace) != (
        btc.max_brti_retrace, btc.require_measurable_retrace)
    assert rule.require_measurable_retrace is False
    assert rule.min_brti_momentum_bps == 0.0
    assert rule.require_momentum_alignment is False


def test_a_full_gold_setup_qualifies_end_to_end():
    ok, facts, failed = kalshi_signal.evaluate(gold_rule(), feats(3.2),
                                               0.70, 600)
    assert ok, failed
    assert [x["name"] for x in facts].count("BRTI distance") == 1


def test_a_gold_setup_outside_the_band_is_refused_end_to_end():
    ok, _facts, failed = kalshi_signal.evaluate(gold_rule(), feats(12.0),
                                                0.70, 600)
    assert not ok
    assert "BRTI distance" in failed


# ------------------------------------------------------- instance isolation

def test_the_launcher_isolates_every_shared_path():
    """Gold must not write BTC's policy, corpus, reference or database. The
    learning runner WRITES the policy path, so a shared one would overwrite
    BTC's live policy at gold's first scheduled fit."""
    ps1 = (ROOT / "scripts" / "run_gold.ps1").read_text(encoding="utf-8")
    for needed in ('BTC15_INSTANCE       = "gold"',
                   'KALSHI_SERIES        = "KXGOLD15M"',
                   'DATABASE_PATH        = "gold15.db"',
                   "runtime-gold/intelligence_policy.json",
                   "runtime-gold/intelligence_candidates.json",
                   "runtime-gold/settlement_reference.db",
                   # Prefix, not the exact filename: the corpus moved to the
                   # merged one on 2026-09-25, the only corpus carrying
                   # brti_retrace. The isolation property is what matters.
                   "data/brti_history_gold",
                   "data/market_data_kxgold15m.db",
                   'TELEGRAM_COMMANDS_ENABLED = "false"'):
        assert needed in ps1, needed


def test_only_one_instance_consumes_telegram_commands():
    """getUpdates is offset-acknowledged, so three readers race for every
    message - including the kill switch. BTC keeps the command stream."""
    for name in ("run_eth.ps1", "run_gold.ps1"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 'TELEGRAM_COMMANDS_ENABLED = "false"' in text


def test_the_three_daily_loss_floors_do_not_silently_triple():
    """Each process carries its own floor; adding an instance at the default
    would raise the combined exposure without anyone deciding to."""
    gold = (ROOT / "scripts" / "run_gold.ps1").read_text(encoding="utf-8")
    assert 'AUTO_DAILY_LOSS_LIMIT = "7"' in gold


def test_gold_is_a_recognised_asset():
    """Not cosmetic. `learning_runner._corpus_mismatch` treats an
    unrecognised series as "no opinion", so an instrument missing from
    `surface.asset` has its corpus guard SILENTLY DISABLED - and gold would
    then have been allowed to fit on BTC rows, which is the one thing that
    guard exists to prevent. It also labels every message."""
    from btc15_signal import surface
    assert surface.asset("KXGOLD15M") == "GOLD"
    assert surface.asset("KXGOLD15M-26SEP242200-00") == "GOLD"
    assert surface.asset("KXBTC15M-26SEP242200-00") == "BTC"


def test_the_corpus_guard_actually_fires_for_gold():
    """A gold instance handed BTC rows must refuse to fit."""
    from btc15_signal.learning_runner import LearningRunner

    class S:
        kalshi_series = "KXGOLD15M"

    runner = object.__new__(LearningRunner)
    runner.settings = S()
    btc_rows = [{"ticker": "KXBTC15M-26SEP242200-00"}] * 5
    gold_rows = [{"ticker": "KXGOLD15M-26SEP242200-00"}] * 5
    assert runner._corpus_mismatch(btc_rows), "gold accepted a BTC corpus"
    assert runner._corpus_mismatch(gold_rows) == ""


def test_decision_records_exists_before_anything_is_written(tmp_path):
    """A BRAND NEW instance must be readable, not only writable.

    `decision_records` used to be created inside `record_decision` - the WRITE
    path - so every reader on a fresh instance failed until the first decision
    happened to be written. The gold instance logged "decision records for
    window ... were NOT graded: no such table" on every settlement from launch
    on 2026-09-24. It is the `_migrate_delivery` bug from the other end: schema
    arriving from somewhere other than startup.
    """
    from btc15_signal.store import Store
    store = Store(str(tmp_path / "fresh.db"))
    tables = {r[0] for r in store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "decision_records" in tables

    # And the read path works on it rather than raising into a log line.
    store.settle_decision_records(1_790_000_000_000, "UP")
