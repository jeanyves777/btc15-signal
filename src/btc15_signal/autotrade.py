"""Guards for unattended execution.

Auto mode places real orders with nobody watching, so the interesting code is
not the part that trades - it is the part that refuses to. Every limit below is
a hard stop evaluated immediately before an order, from durable state, so a
restart cannot reset a breached limit back to zero.

The design rule: a guard may only ever *block* a trade. Nothing here can cause
one, and if any check cannot be evaluated the answer is "no".
"""

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class AutoLimits:
    daily_loss_limit: float
    max_trades_per_day: int
    max_trades_per_hour: int
    min_seconds_between: int
    budget: float


@dataclass(frozen=True)
class AutoState:
    """What has already happened today, read fresh before each decision."""

    trades_today: int
    trades_last_hour: int
    seconds_since_last: float
    realised_today: float
    open_positions: int


def auto_block_reason(
    limits: AutoLimits, state: AutoState, price: float, enabled: bool
) -> str:
    """Why this order must not be placed automatically, or "" to proceed.

    Ordered so the most serious condition is reported first: a breached loss
    limit is a different message from merely trading too fast.
    """
    if not enabled:
        return "auto trading is off"
    if state.realised_today <= -abs(limits.daily_loss_limit):
        return (
            f"daily loss limit reached: {state.realised_today:+.2f} against a "
            f"{-abs(limits.daily_loss_limit):.2f} floor"
        )
    if state.open_positions > 0:
        # One at a time. Concurrent unattended positions multiply a bad streak
        # and there is nobody awake to notice the first one going wrong.
        return f"{state.open_positions} position(s) already open"
    if state.trades_today >= limits.max_trades_per_day:
        return f"{state.trades_today} trades today, limit {limits.max_trades_per_day}"
    if state.trades_last_hour >= limits.max_trades_per_hour:
        return f"{state.trades_last_hour} trades this hour, limit {limits.max_trades_per_hour}"
    if state.seconds_since_last < limits.min_seconds_between:
        return (
            f"only {state.seconds_since_last:.0f}s since the last order, "
            f"minimum {limits.min_seconds_between}s"
        )
    if not 0 < price < 1:
        return f"implausible price {price}"
    if limits.budget <= 0:
        return "order budget is zero"
    return ""


def day_key(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, UTC).strftime("%Y-%m-%d")
