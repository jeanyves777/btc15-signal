"""The conditional recovery add-on: one extra contract, resting, conditional.

WHAT IT IS. When recovery is active and a base position is open, rest a BUY for
one more contract 2c below the actual base fill, and keep it alive only while
the BRTI evidence that justified the trade still holds. Cancel it the moment it
does not.

WHY IT IS CONDITIONAL RATHER THAN A PLAIN LIMIT. Measured on 1,913 BRTI-native
entries over the corpus:

    add fires, unconditioned, 2c dip   n=1143  win 65.9%   -0.0043/contract
    add fires, CONDITIONS HOLD         n= 202  win 81.7%   +0.0621 [+0.0087, +0.1159]
    conditions BLOCK the add           n= 941  win 62.5%   -0.0185

Unconditioned, the resting order fills on 97.5% of eventual losers and only
30% of eventual winners, because a position that never gets cheaper wins 99.2%
(FINDINGS 37's 99.3%/54.1% split, reproduced). A cheap price is usually the
market telling you the trade is going wrong. The conditions are what separate a
2c flutter from deterioration, and they are the entire reason this is not a
martingale.

WHAT IS NOT ESTABLISHED, and the operator's own framing is the right one:
launch readiness means correct execution and bounded exposure. **Profitability
is what the live test decides.** The backtest cannot settle it because a minute
candle cannot tell a momentary touch from a fill: it records that the ask
REACHED the limit, not that a resting order at the back of a queue actually
traded. That is why every record here carries queue position, touch duration
and the real fee, and why the second contract's P&L is tracked separately.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from .brti import BRTIFeatures

# One namespace for every recovery add, so the id is a pure function of the
# position. Two evaluations of the same position produce the same id, and
# Kalshi rejects the duplicate - which is what makes a retry safe.
ADD_NAMESPACE = "btc15.recovery-add"


class AddState(StrEnum):
    BASE_ENTERED = "BASE ENTERED"
    PENDING = "RECOVERY ADD PENDING"
    EXECUTED = "RECOVERY ADD EXECUTED"
    SKIPPED = "RECOVERY ADD SKIPPED"
    CANCELLED = "RECOVERY ADD CANCELLED"
    # NOT A VERDICT. The position gets ONE evaluation - `_step` returns on any
    # existing row - so a refusal is permanent for that market. That is right
    # for a decision and wrong for a question that could not be asked yet: on
    # 2026-09-23 all 25 of the day's refusals were "crossing history
    # unavailable", every one of them because BRTI's series trailed the entry
    # instant by 0-2 seconds, and every one of them would have been answerable
    # on the next poll. DEFERRED records that the question is still open and is
    # the one state `_step` will re-enter.
    DEFERRED = "RECOVERY ADD DEFERRED"


def client_order_id(ticker: str, side: str, window_open_ms: int) -> str:
    """Deterministic, so a retry or a restart cannot open a second contract."""
    return str(uuid5(NAMESPACE_URL, f"{ADD_NAMESPACE}:{ticker}:{side}:{window_open_ms}"))


@dataclass(frozen=True)
class AddDecision:
    place: bool
    reason: str
    price: float | None = None
    failed: tuple[str, ...] = field(default_factory=tuple)

    @property
    def state(self) -> AddState:
        return AddState.PENDING if self.place else AddState.SKIPPED


@dataclass(frozen=True)
class AddLimits:
    """Bounded exposure, checked before anything is placed."""

    max_total_funding: float = 30.0
    max_contracts_per_position: int = 1  # the ADD, on top of the base
    dip_cents: float = 0.02
    min_seconds_remaining: int = 120  # the add-entry deadline
    distance_floor: float = 10.0


def conditions(
    features: BRTIFeatures,
    entry_side: str,
    crossed_since_entry: bool,
    limits: AddLimits,
) -> tuple[bool, tuple[str, ...]]:
    """The operator's condition list, as predicates. Returns (ok, failures).

    Every one reads BRTI or the clock. None reads Binance - the whole point is
    that the evidence comes from the instrument the contract settles on.
    """
    failed: list[str] = []
    if features.stale:
        failed.append("reference stale")
    if features.side != entry_side:
        failed.append(f"BRTI direction flipped to {features.side}")
    if crossed_since_entry:
        failed.append("BRTI crossed the strike since entry")
    direction = 1 if entry_side == "UP" else -1
    if direction * features.brti_momentum_bps <= 0:
        failed.append(
            f"momentum not aligned ({direction * features.brti_momentum_bps:+.1f} bps)"
        )
    if features.brti_normalized_distance < limits.distance_floor:
        failed.append(
            f"distance collapsed to {features.brti_normalized_distance:.1f}x "
            f"(needs {limits.distance_floor:g}x)"
        )
    return (not failed), tuple(failed)


def max_net_profit(count: int, average_cost: float, fee) -> float:
    """What the COMBINED position can still make, net of the entry fee."""
    return count * (1.0 - average_cost) - fee(average_cost, count)


def evaluate(
    *,
    features: BRTIFeatures,
    entry_side: str,
    entry_fill: float,
    current_ask: float | None,
    crossed_since_entry: bool,
    remaining_s: int,
    required_per_trade: float,
    recovery_active: bool,
    already_added: bool,
    open_exposure: float,
    limits: AddLimits,
    fee,
) -> AddDecision:
    """Should the add rest right now? Pure, so it is testable without a broker.

    Order matters. The cheap, certain refusals come first so a log line names
    the real reason rather than the first expensive thing that happened to
    fail.
    """
    if not recovery_active:
        return AddDecision(False, "recovery is not active")
    if already_added:
        return AddDecision(False, "one add per position, already used")
    if remaining_s < limits.min_seconds_remaining:
        return AddDecision(
            False, f"past the add deadline ({remaining_s}s < "
                   f"{limits.min_seconds_remaining}s)"
        )
    if current_ask is None or not 0 < current_ask < 1:
        return AddDecision(False, "no executable ask")

    limit_price = round(entry_fill - limits.dip_cents, 4)
    if limit_price <= 0:
        return AddDecision(False, "limit price would be non-positive")

    ok, failed = conditions(features, entry_side, crossed_since_entry, limits)
    if not ok:
        return AddDecision(False, "; ".join(failed), limit_price, failed)

    # Bounded exposure. An unknown exposure is treated as no room, never as
    # room - `resting_exposure` returns -1 when it could not be read.
    if open_exposure < 0:
        return AddDecision(False, "exposure unknown; refusing to add", limit_price)
    if open_exposure + limit_price > limits.max_total_funding:
        return AddDecision(
            False,
            f"would exceed the ${limits.max_total_funding:.0f} cap "
            f"(${open_exposure:.2f} committed)",
            limit_price,
        )

    # The combined position has to be able to pay the recovery portion, or the
    # add is buying exposure that cannot do the job it exists for.
    combined = round((entry_fill + limit_price) / 2, 6)
    potential = max_net_profit(2, combined, fee)
    if potential < required_per_trade:
        return AddDecision(
            False,
            f"combined average {combined:.4f} offers {potential:+.4f}, "
            f"recovery needs {required_per_trade:+.4f}",
            limit_price,
        )
    return AddDecision(
        True,
        f"resting 1 at {limit_price:.2f} ({limits.dip_cents * 100:.0f}c below "
        f"the {entry_fill:.2f} fill); combined {combined:.4f} could make "
        f"{potential:+.4f} against {required_per_trade:+.4f} needed",
        limit_price,
    )


def should_cancel(
    *,
    features: BRTIFeatures | None,
    entry_side: str,
    crossed_since_entry: bool,
    remaining_s: int,
    recovery_active: bool,
    base_position_open: bool,
    limits: AddLimits,
    recovery_owes: bool = False,
) -> tuple[bool, str]:
    """A resting add must be killed the moment its justification goes.

    Deliberately independent of `evaluate`: the reasons to CANCEL are not the
    negation of the reasons to place. A position that has exited, or a recovery
    that has completed, invalidates the order regardless of what BRTI is doing.
    """
    if not base_position_open:
        return True, "base position is no longer open"
    if not recovery_active:
        # TWO WAYS TO BE INACTIVE, and they are not the same event. "Completed"
        # means the money came back; a base-only cycle still owes it and only
        # the sizing ended. Recording the wrong one would put a false
        # repayment in the add-on's audit trail.
        return True, (
            "recovery sizing ended; deficit still outstanding"
            if recovery_owes else "recovery completed"
        )
    if remaining_s < limits.min_seconds_remaining:
        return True, "past the add-entry deadline"
    if features is None:
        return True, "no BRTI reference available"
    ok, failed = conditions(features, entry_side, crossed_since_entry, limits)
    if not ok:
        return True, "; ".join(failed)
    return False, ""
