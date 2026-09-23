"""When recovery SIZING ends, before the deficit is repaid.

THE OPERATOR'S RULE, as specified.

    End recovery sizing when BOTH conditions are met:
      * four profitable, fully closed market positions since the cycle began
      * at least 50% of the cycle's INITIAL deficit recovered, net of fees
        and subsequent realised losses

Both, not either. Four wins that have barely moved the deficit leave real
ground to make up; half the money back after one lucky market says nothing
about whether the run is stable. The rule fires where the two agree.

    "This is an exposure-reduction rule - not a claim that a loss becomes
     more likely after four wins."

That framing matters for how the code is written. Nothing here predicts
anything. It caps how long the account carries doubled size, which is a
statement about exposure, not about the market.

COUNTING.

  * A MARKET counts once. Base and add-on fills on the same ticker are one
    position with one outcome, and counting contracts or fills would reach
    four on a single market that happened to be filled twice.
  * Wins need not be consecutive. A loss in between does not reset the count.
  * Progress counts EVERY realised trade, base-size or upsized alike. The
    ledger does not record which size won the money back and it does not
    matter.
  * The denominator is the cycle's INITIAL deficit - the hole this cycle
    opened with. Subsequent losses reduce the measured progress because they
    add to what is still owed, which is what "net of subsequent realised
    losses" means.

WHAT ENDING MEANS. The UPSIZE stops. The deficit is preserved in the ledger,
is not erased, and is never announced as a full recovery. Base-size profits
clear the remainder. A new loss in that base-only phase is recorded in the
deficit but does NOT reactivate sizing or reset the win counter - reactivating
is the loop this exists to prevent. When the deficit genuinely reaches zero
the cycle closes, and a later loss opens a fresh one.

Full recovery remains an immediate end in its own right, even before four
wins: there is nothing left to size for.
"""

from dataclasses import dataclass

# Fraction of the cycle's INITIAL deficit that must be back. The operator set
# 50% as the default and 40-60% as the range within which it may be tuned;
# values outside that are refused rather than silently clamped, because a
# threshold nobody intended is worse than an error.
EXIT_FRACTION = 0.50
FRACTION_MIN = 0.40
FRACTION_MAX = 0.60
REQUIRED_WINS = 4


@dataclass(frozen=True)
class ExitDecision:
    end_sizing: bool
    reason: str
    recovered_fraction: float = 0.0
    wins: int = 0

    def __bool__(self) -> bool:
        return self.end_sizing


def recovered_fraction(initial: float, deficit: float) -> float:
    """How much of the cycle's opening hole is back, as 0.0-1.0.

    Net of subsequent losses by construction: a loss raises `deficit`, which
    lowers this. Clamped at zero so a cycle now deeper than it started reads
    as no progress rather than a negative percentage.
    """
    if initial <= 0:
        return 0.0
    return max(0.0, min(1.0, (initial - max(0.0, deficit)) / initial))


def validate_fraction(fraction: float) -> float:
    """Inside the operator's 40-60% range, or raise."""
    if not FRACTION_MIN <= fraction <= FRACTION_MAX:
        raise ValueError(
            f"recovery exit fraction {fraction} is outside the "
            f"{FRACTION_MIN:.0%}-{FRACTION_MAX:.0%} range the operator set"
        )
    return fraction


def decide(
    initial: float,
    deficit: float,
    wins: int,
    *,
    exit_fraction: float = EXIT_FRACTION,
    required_wins: int = REQUIRED_WINS,
    enabled: bool = True,
) -> ExitDecision:
    """Should recovery SIZING end now? Pure, so replay and live agree.

    `wins` is a count of distinct profitable closed MARKETS in this cycle.
    """
    fraction = recovered_fraction(initial, deficit)
    if not enabled:
        return ExitDecision(False, "partial exit disabled", fraction, wins)
    if initial <= 0 or deficit <= 0:
        # Nothing owed. Full recovery ends the cycle on its own path, and
        # saying "ended early" there would confuse repaid with stood down.
        return ExitDecision(False, "", fraction, wins)
    if wins < required_wins or fraction < exit_fraction:
        return ExitDecision(False, "", fraction, wins)
    return ExitDecision(
        True,
        f"{wins} winning markets and {fraction:.0%} of the "
        f"${initial:,.2f} deficit recovered",
        fraction,
        wins,
    )
