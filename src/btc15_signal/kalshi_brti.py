"""The `kalshi_brti` entry rule: every gate computed on the settlement price.

SHADOW. Nothing here places an order, and there is deliberately no flag that
makes it. Promoting it is a code change someone has to make on purpose, the
same contract the hourly ladder runs under.

WHY IT IS A SEPARATE RULE RATHER THAN A SETTING ON THE OLD ONE. The deployed
`EntryRule` gates on `min_normalized_distance = 1.5`, a number measured against
Binance RAW volatility. BRTI is published as a trailing 60-second mean, which
is a low-pass filter, so the identical market reads 10-20 on this instrument
(FINDINGS 43). Sharing a field between the two would let a threshold measured
for one be applied to the other by autocomplete, silently turning the gate off.
Different quantity, different rule, different file.

THE THRESHOLD WAS MEASURED, NOT TRANSLATED. `scripts/measure_brti_gates.py`
over 2,145 corpus markets, 1,913 in-band entries, net of fees:

    BRTI normalized distance    n      net/contract     95% CI
    < 5                        419        -0.0291   [-0.0719, +0.0142]
    5 - 10                     919        -0.0004   [-0.0281, +0.0258]
    10 - 15                    427        +0.0331   [-0.0012, +0.0665]
    15 - 20                    117        +0.0919   [+0.0407, +0.1359]

    floor >= 10  vs taking everything:   +0.0398   [+0.0126, +0.0668]
    floor >= 15  vs taking everything:   +0.0816   [+0.0361, +0.1213]

Positive in all four chronological quarters (+0.048, +0.055, +0.013, +0.046),
which is the check FINDINGS 14 used to kill a sub-band that only looked good
pooled.

WHAT IS NOT ESTABLISHED, and why this stays in shadow:

* The headline BRTI-native edge is **+0.0077/contract, CI [-0.0114, +0.0254]** -
  it spans zero. The DISTANCE STRUCTURE is what clears zero, not the rule.
* The floor was chosen by scanning six values. That is six chances to look
  good, and the interval is not corrected for it.
* The threshold moved with sample size while the backfill ran (n=886 -> 1,469
  -> 1,913). A number that is still drifting has not settled.

So this records what it WOULD have done and is graded against what the
deployed rule actually did. It earns a promotion by measurement or not at all.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from .brti import BRTIFeatures


@dataclass(frozen=True)
class KalshiBRTIRule:
    """Entry gates on BRTI. Field names deliberately unlike `EntryRule`'s."""

    enabled: bool = False  # shadow; nothing reads this to place an order
    min_ask: float = 0.70
    max_ask: float = 0.93
    # The measured floor. NOT `min_normalized_distance` - that name belongs to
    # the Binance quantity and means something six times smaller.
    min_brti_normalized_distance: float = 10.0
    # AN ABSOLUTE DISTANCE BAND, IN BPS, FOR INSTRUMENTS WHERE THE NORMALISED
    # ONE DOES NOT MEAN WHAT IT MEANS ON BTC. Both None keeps the normalised
    # floor, so BTC and ETH are untouched.
    #
    # Gold needs this and the reason is measured (FINDINGS 73). Its per-second
    # reference jitters about seven times more than BRTI in bps, but that
    # jitter MEAN-REVERTS - only 4.0% of it survives to settlement against
    # BTC's 30.5%. `brti_normalized_distance` divides by that jitter, so gold's
    # median reads 0.83 where BTC's reads 6.92 while the two are equivalent in
    # risk-adjusted terms (distance/actual-move 0.74x against 0.78x). The
    # normalised gate is therefore not measuring distance on gold; it is
    # measuring feed noise, and the deployed 10-15x floor rejects every setup.
    #
    # It is a BAND, not a floor, which is the other way gold differs. Gold's
    # edge lives at 1.5-6.0bp and is gone above it: 2-5bp scores +0.0715
    # [+0.0344, +0.1078] while 5-10bp scores +0.0074 spanning zero. Level-
    # maintenance is what pays here - gold holds its side 75.4% at 2-5bp
    # against BTC's 66.0% - and a large distance means the move already
    # happened, which is BTC's edge and not gold's.
    min_abs_distance_bps: float | None = None
    max_abs_distance_bps: float | None = None
    # CEILINGS, for instruments whose data asks for one. None keeps the gate a
    # pure floor, so BTC and ETH are unchanged.
    #
    # The direction is an instrument fact, not a convention. BTC wants momentum
    # and acceleration HIGH - its edge is a move that already happened and left
    # the strike behind. Silver and SOL want them LOW: silver scores -0.0745 at
    # |momentum| >= 10bp and -0.0657 at accel >= +10, both negative, while the
    # quiet buckets carry the edge. Gold is the same shape (accel -10..0 beats
    # 0..+10, +0.1474 against +0.0754).
    #
    # Copying BTC's floor onto those instruments would gate for the opposite of
    # what their data supports - which is how a threshold measured on one
    # instrument silently becomes a wrong-way gate on another.
    max_brti_momentum_bps: float | None = None
    max_brti_accel: float | None = None
    # WHETHER AN UNMEASURABLE RETRACE IS A REFUSAL.
    #
    # True is BTC's behaviour and stays the default: `brti_retrace` is None
    # when the recent window holds no advance to give back, and on BTC that
    # means the move which built the cushion cannot be shown to be alive. The
    # gate refuses it deliberately - "looks fine" and "was checked" are
    # different states.
    #
    # On GOLD that reading inverts. Gold's edge IS level-maintenance: price
    # steps away from the strike and then HOLDS, which is precisely a window
    # with no advance in it. So None is not a missing measurement there, it is
    # the target state, and refusing it would reject the setups the edge was
    # measured on. Gold's +0.0987 residual was measured with no retrace gate at
    # all (FINDINGS 73), so this only stops the gate contradicting the evidence
    # it was shipped beside.
    #
    # A MEASURED retrace still gates normally on both: this changes what
    # happens when there is nothing to measure, never what happens when there
    # is.
    require_measurable_retrace: bool = True
    # THE INSTRUMENT THIS RULE IS FOR. Used for wording only - "BRTI stability"
    # on a gold alert names Bitcoin's reference index on a market that has
    # never touched it. The gate reads the gold feed (verified: gold-scale
    # values, zero cross-contamination either way), so this was a label that
    # misdescribed correct data.
    asset: str = ""
    # THE REVERSAL GATE. 1.0 means the whole recent advance has been
    # handed back. Shipped blocking on the operator's instruction; the
    # evidence behind the threshold is in FINDINGS 58 and is thin.
    max_brti_retrace: float = 0.60
    # Momentum is UNGATED until it is measured on BRTI. A 60-second mean lags
    # the raw series, so the Binance momentum thresholds describe a different
    # quantity here too, and an unmeasured gate is worse than no gate because
    # it looks deliberate.
    min_brti_momentum_bps: float = 0.0
    require_momentum_alignment: bool = False
    # MOMENTUM SCALES THE DISTANCE REQUIRED. The operator's reading, 2026-09-24:
    # "a lot of momentum can mean distance can revert fast", so momentum should
    # set how far you must be from the strike rather than which way you bet.
    #
    # Measured on 2,145 corpus markets sampled 660-360s, priced on the Kalshi
    # book, day-clustered over 68 days:
    #
    #   deployed  >=10x any momentum      n=4350  +0.0268  [+0.012, +0.041]
    #   proposed  calm >=10x, moving >=15x n=2861 +0.0364  [+0.020, +0.051]
    #
    # with the out-of-sample half BETTER than the whole (+0.0380). Non-calm
    # setups at 10x return +0.0160 with a lower bound of exactly zero; they
    # need 15x before the interval clears it.
    #
    # WHAT WAS MEASURED AND REJECTED: lowering the floor FOR calm setups. Edge
    # rises with the floor even when calm (+0.0318 at 4x, +0.0560 at 15x), and
    # the marginal band below 10x spans zero in every cut - 7-10x calm is
    # +0.0242 [-0.006, +0.052]. So this raises the bar on moving markets and
    # never lowers it on quiet ones.
    #
    # ALIGNMENT IS STILL OFF, and this is not it. Aligned +0.027 against
    # against +0.026 on BRTI: the Binance-era separation (84.2% vs 71.4%)
    # does not reproduce. Magnitude is the thing that carries; direction is not.
    calm_momentum_bps: float = 5.0
    moving_min_brti_normalized_distance: float = 15.0
    # THE THREE LEVEL-HOLDING GATES. Operator's decision, 2026-09-24, after
    # RSI failed: "we are mostly already in the money; will price stay above
    # the level we need it to stay". Direction at 15 minutes is already in
    # the price - whether the STRIKE is being defended is not.
    #
    # Measured on the bot's own 176 in-the-money executed trades, priced on
    # Kalshi's settlement P&L. Each alone:
    #
    #   accel >= -5       keeps 91%   +5.09 against +1.07 actual
    #   rejections >= 2   keeps 38%   +5.75
    #   held_s >= 300     keeps 27%   +5.00
    #
    # and together at the shipped settings: keeps 34%, 89.8% win, +9.21.
    #
    # HELD_S IS NOT MONOTONIC and the threshold is chosen knowing it: alone
    # at 120s and 180s it HURTS (-1.38, -2.75) and only helps at 300s. It
    # earns its place here in combination, not on its own, which is recorded
    # against it rather than hidden.
    #
    # THE SAMPLE IS 176 TRADES OVER 5 DAYS and the cell is the best of 36
    # swept combinations, with a trade-level rather than day-clustered
    # interval. That is weaker evidence than the distance floor rests on.
    #
    # THE WIDER TEST CONTRADICTED IT, and that is recorded here because the
    # gates remain live. On 5,546 corpus decision points across 68 days,
    # day-clustered, inside the deployed price band and scaled distance floor:
    #
    #   deployed gates only      n=5546  88.3%  +0.0374  [+0.0225, +0.0512]
    #   + these three gates      n=1628  87.2%  +0.0390  [+0.0166, +0.0606]
    #   what they REFUSE         n=3918  88.8%  +0.0368  [+0.0199, +0.0526]
    #
    # They refuse 71% of setups and what they refuse scores the same as what
    # they keep. `accel` points the WRONG WAY on that sample - decaying
    # setups returned +0.0410 against +0.0296 for building ones, the reverse
    # of the live result. `held_s < 120` binds 9 times in 5,546.
    # `rejections` is non-monotonic: 1 -> +0.0366, 2 -> +0.0434,
    # 3+ -> +0.0245 spanning zero.
    #
    # KEPT LIVE BY THE OPERATOR'S DECISION, 2026-09-24, with that evidence in
    # view. The recommendation on the table was archive-only. The features
    # are recorded on every decision either way, so this stays re-measurable
    # on live data under the current rules rather than re-arguable - which is
    # the only reason shipping against a corpus result is recoverable.
    min_brti_accel: float = -5.0
    min_brti_held_s: float = 120.0
    min_brti_rejections: int = 2
    entry_from_seconds: int = 660
    entry_to_seconds: int = 360

    def _distance_fact(self, features, floor: float, regime: str) -> dict:
        """The distance test, in whichever units this instrument is measured.

        Two regimes, and which one applies is configuration, not a guess. With
        no absolute band set the rule is BTC's: distance must exceed a floor
        expressed in volatility units, because on BTC a large normalised
        distance means the move has already happened and the strike is a long
        way behind it.

        With a band set, the rule is gold's: distance must lie INSIDE a range
        in bps. The difference is not cosmetic - on gold the edge comes from
        the level holding, not from a move having happened, so a distance
        beyond the band is evidence against the trade rather than for it.
        Above 6bp gold's residual collapses to zero (FINDINGS 73).
        """
        band = (self.min_abs_distance_bps is not None
                or self.max_abs_distance_bps is not None)
        if not band:
            return {
                "passed": features.brti_normalized_distance >= floor,
                "pass_text": (
                    f"{features.brti_normalized_distance:.1f}x BRTI vol "
                    f"(needs {floor:.0f}x, {regime})"
                ),
                "fail_text": (
                    f"{features.brti_normalized_distance:.1f}x - needs "
                    f"{floor:.0f}x ({regime} market, "
                    f"{abs(features.brti_momentum_bps or 0.0):.1f} bps)"
                ),
                "value": features.brti_normalized_distance,
            }
        lo = self.min_abs_distance_bps
        hi = self.max_abs_distance_bps
        gap = abs(getattr(features, "signed_distance_bps", 0.0) or 0.0)
        # An unmeasurable gap is refused, not waved through: "cannot be
        # checked" and "is fine" are different states everywhere in this rule.
        ok = (lo is None or gap >= lo) and (hi is None or gap <= hi)
        window = (f"{lo:.1f}-{hi:.1f}bp" if lo is not None and hi is not None
                  else f">= {lo:.1f}bp" if hi is None else f"<= {hi:.1f}bp")
        if ok:
            fail = ""
        elif hi is not None and gap > hi:
            fail = f"{gap:.1f}bp - beyond the {window} band; the move already happened"
        else:
            fail = f"{gap:.1f}bp - inside the {window} band's floor"
        return {
            "passed": ok,
            "pass_text": f"{gap:.1f}bp from target (band {window})",
            "fail_text": fail,
            "value": gap,
        }

    def distance_floor_for(self, momentum_bps: float | None) -> tuple[float, str]:
        """(floor, why) - the distance this setup must clear, and the reason.

        Returned together so the alert can say WHICH floor applied. A gate
        whose threshold moves without saying so reads as an inconsistent gate.
        """
        magnitude = abs(momentum_bps or 0.0)
        if magnitude < self.calm_momentum_bps:
            return self.min_brti_normalized_distance, "calm"
        # NEVER BELOW THE CALM FLOOR. These are two independent fields, so a
        # config could set the moving floor lower than the calm one and a
        # busy market would need LESS cushion than a quiet one - the exact
        # inverse of what was measured. Worse, raising
        # `min_brti_normalized_distance` alone would then stop tightening the
        # gate at all, which is how a test that sets it to 1e9 still saw
        # setups pass.
        return max(self.min_brti_normalized_distance,
                   self.moving_min_brti_normalized_distance), "moving"

    @classmethod
    def load(cls, path: str | Path) -> "KalshiBRTIRule":
        source = Path(path)
        if not source.exists():
            return cls()
        data = json.loads(source.read_text())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def ask_for(self, features: BRTIFeatures, yes_bid: float, yes_ask: float) -> float:
        """The price of the side BRTI says is winning.

        The side comes from the official reference; the price comes from the
        Kalshi book. Long UP pays the yes ask, long DOWN pays 1 - yes bid.
        """
        return yes_ask if features.side == "UP" else round(1 - yes_bid, 4)

    def check_facts(self, features: BRTIFeatures, ask: float,
                    remaining_s: int) -> list[dict]:
        """The gates, each marked with whether it can actually fail.

        A THRESHOLD NO VALUE CAN FAIL IS NOT A PASSED CHECK. On gold,
        2026-09-25, an alert read "Entry checks 8/8" with five green ticks
        whose thresholds were `accel >= -1e9`, `held >= 0s`, `rejections >= 0`,
        `momentum >= 0.0` and `retrace <= 1.0`. Only three of the eight could
        fail on any input. Counting the other five as protections passed
        overstated the test by more than double, and the operator read the
        message exactly as it was written.

        So each fact now carries `enabled`. A disabled gate still shows its
        value - the number is real and worth seeing - but it is reported as
        DISABLED, never as a tick, and never counted.
        """
        facts = self._gate_facts(features, ask, remaining_s)
        for fact in facts:
            fact.setdefault("enabled", self.gate_binds(str(fact["name"])))
        return facts

    # A threshold no value can fail. Derived from the config, because that is
    # what decides it - a gate is inert for every input or none of them.
    #
    # -1e6 rather than an equality against -1e9: the sentinel has been written
    # as -1000000000.0, -1e9 and -999999999 at different times and an equality
    # check would quietly call one of those a live gate.
    def gate_binds(self, name: str) -> bool:
        """Can this gate refuse anything, as configured?"""
        if name == "Decision ask":
            return self.min_ask > 0.0 or self.max_ask < 1.0
        if name == "BRTI distance":
            return bool(self.min_brti_normalized_distance > 0.0
                        or self.moving_min_brti_normalized_distance > 0.0
                        or self.min_abs_distance_bps is not None
                        or self.max_abs_distance_bps is not None)
        if name == "Move still working":
            return bool(self.min_brti_accel > -1e6
                        or self.max_brti_accel is not None)
        if name == "Level held":
            return self.min_brti_held_s > 0.0
        if name == "Level tested":
            return self.min_brti_rejections > 0
        if name == "BRTI momentum":
            return bool(self.min_brti_momentum_bps > 0.0
                        or self.max_brti_momentum_bps is not None
                        or self.require_momentum_alignment)
        if name == "BRTI stability":
            return bool(self.max_brti_retrace < 1.0
                        or self.require_measurable_retrace)
        # `Reference` is staleness and sample count - never configurable off.
        return True

    def _gate_facts(
        self, features: BRTIFeatures, ask: float, remaining_s: int
    ) -> list[dict]:
        """The gates, each as a named fact, in the shape `messages` renders.

        Every one reads BRTI or the Kalshi book. None reads Binance.
        """
        direction = 1 if features.side == "UP" else -1
        aligned = direction * features.brti_momentum_bps
        floor, regime = self.distance_floor_for(features.brti_momentum_bps)
        band = f"{self.min_ask * 100:.0f}-{self.max_ask * 100:.0f}c"
        return [
            {
                "name": "Decision ask",
                "passed": self.min_ask <= ask <= self.max_ask,
                "pass_text": f"{ask * 100:.0f}c within {band}",
                "fail_text": f"{ask * 100:.0f}c - needs {band}",
                "value": ask,
            },
            {
                # THE FLOOR MOVES WITH MOMENTUM, and says which one applied.
                # A quiet market needs less cushion than a fast one, because
                # a fast one can erase the cushion before expiry.
                "name": "BRTI distance",
                **self._distance_fact(features, floor, regime),
            },
            {
                # THE MOVE THAT BUILT THE CUSHION - still building, or spent?
                # The trades this refuses won 40 of 55 and still lost 6.93:
                # they win often and lose big.
                "name": "Move still working",
                # A FLOOR ON BTC, A BAND WHERE THE DATA ASKS FOR ONE. Silver
                # scores -0.0657 at accel >= +10 and gold's -10..0 bucket
                # (+0.1474) beats its 0..+10 (+0.0754): on those instruments a
                # move still accelerating hard is evidence against, not for.
                "passed": (features.brti_accel is None
                           or (features.brti_accel >= self.min_brti_accel
                               and (self.max_brti_accel is None
                                    or features.brti_accel
                                    <= self.max_brti_accel))),
                # A NEGATIVE NUMBER UNDER "Move still working" with a green
                # tick reads as a contradiction: the label says the move is
                # alive, the figure says it is decaying. Both are true - it is
                # decaying WITHIN tolerance - and the text now says which.
                "pass_text": (
                    "not measurable" if features.brti_accel is None
                    else (f"{features.brti_accel:+.1f} bps accel"
                          if features.brti_accel >= 0
                          else f"{features.brti_accel:+.1f} bps - easing, "
                               f"within {self.min_brti_accel:+.0f} tolerance")
                ),
                # The wording has to name WHICH end failed. A ceiling breach
                # reported as "the move is decaying" says the opposite of what
                # happened.
                "fail_text": (
                    "not measurable" if features.brti_accel is None
                    else (f"{features.brti_accel:+.1f} bps - accelerating too "
                          f"hard (max {self.max_brti_accel:+.0f})"
                          if (self.max_brti_accel is not None
                              and features.brti_accel > self.max_brti_accel)
                          else f"{features.brti_accel:+.1f} bps - the move is "
                               f"decaying (needs {self.min_brti_accel:+.0f})")
                ),
                "value": features.brti_accel,
            },
            {
                # HOW LONG THE LEVEL HAS ACTUALLY HELD.
                "name": "Level held",
                "passed": (features.brti_held_s is None
                           or features.brti_held_s >= self.min_brti_held_s),
                "pass_text": (
                    f"{features.brti_held_s:.0f}s on side "
                    f"(needs {self.min_brti_held_s:.0f}s)"
                    if features.brti_held_s is not None else "not measurable"
                ),
                "fail_text": (
                    f"only {features.brti_held_s:.0f}s on side - needs "
                    f"{self.min_brti_held_s:.0f}s"
                    if features.brti_held_s is not None else "not measurable"
                ),
                "value": features.brti_held_s,
            },
            {
                # TESTED AND HELD beats never approached: a level price has
                # turned back from is evidence, one it never reached is not.
                "name": "Level tested",
                "passed": (features.brti_rejections is None
                           or features.brti_rejections
                           >= self.min_brti_rejections),
                "pass_text": (
                    f"{features.brti_rejections} rejection(s) held"
                    if features.brti_rejections is not None
                    else "not measurable"
                ),
                "fail_text": (
                    f"{features.brti_rejections} rejection(s) - needs "
                    f"{self.min_brti_rejections}"
                    if features.brti_rejections is not None
                    else "not measurable"
                ),
                "value": features.brti_rejections,
            },
            {
                "name": "BRTI momentum",
                # The ceiling is the instrument-specific half. Silver loses at
                # |momentum| >= 10bp (-0.0745) and gold's best bucket is
                # |mom| < 2bp (+0.1423) - quiet is what pays there, where BTC
                # wants the move.
                "passed": (
                    (not self.require_momentum_alignment
                     or aligned >= self.min_brti_momentum_bps)
                    and (self.max_brti_momentum_bps is None
                         or abs(features.brti_momentum_bps or 0.0)
                         <= self.max_brti_momentum_bps)
                ),
                "pass_text": (
                    f"{aligned:+.1f} bps"
                    + (f" (max {self.max_brti_momentum_bps:.0f})"
                       if self.max_brti_momentum_bps is not None else "")
                ),
                "fail_text": (
                    f"{aligned:+.1f} bps - too fast, max "
                    f"{self.max_brti_momentum_bps:.0f}bp"
                    if (self.max_brti_momentum_bps is not None
                        and abs(features.brti_momentum_bps or 0.0)
                        > self.max_brti_momentum_bps)
                    else f"{aligned:+.1f} bps - not aligned"
                ),
                "value": aligned,
            },
            {
                "name": "BRTI stability",
                # None is NOT a pass. A setup whose recent path cannot be
                # measured is one we cannot say is stable, and the whole point
                # of this gate is that "looks fine" and "was checked" are
                # different states.
                "passed": (
                    (features.brti_retrace <= self.max_brti_retrace)
                    if features.brti_retrace is not None
                    # Nothing to give back. A refusal on BTC, where it means
                    # the move cannot be shown alive; the target state on an
                    # instrument whose edge is holding a level.
                    else not self.require_measurable_retrace
                ),
                "pass_text": (
                    "holding"
                    if features.brti_retrace is None
                    else f"{features.brti_retrace * 100:.0f}% given back "
                         f"(max {self.max_brti_retrace * 100:.0f}%)"
                ),
                "fail_text": (
                    "recent move not measurable"
                    if features.brti_retrace is None
                    else f"{features.brti_retrace * 100:.0f}% of the move "
                         f"already reversed"
                ),
                "value": features.brti_retrace,
            },
            {
                "name": "Reference",
                "passed": not features.stale,
                "pass_text": (
                    f"Kalshi BRTI, {features.samples} pts, "
                    f"${features.value:,.2f} vs target ${features.target:,.2f}"
                ),
                "fail_text": "Kalshi BRTI stale",
                "value": features.value,
            },
        ]

    def matches(
        self, features: BRTIFeatures, ask: float, remaining_s: int
    ) -> tuple[bool, str]:
        """(qualifies, why not). Shadow verdict only."""
        if not self.entry_to_seconds <= remaining_s <= self.entry_from_seconds:
            return False, f"outside the entry window at {remaining_s}s"
        if features.stale:
            return False, "BRTI reference is stale"
        for fact in self.check_facts(features, ask, remaining_s):
            if not fact["passed"]:
                return False, f"{fact['name']}: {fact['fail_text']}"
        return True, ""
