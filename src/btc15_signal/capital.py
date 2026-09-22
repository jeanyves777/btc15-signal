"""One sizing authority for every order: base entries and recovery adds alike.

WHY ONE. Two independent rules that both change size is how a cap is exceeded
by the sum of two things each of which looked bounded. On 2026-09-22 the
confidence band and the recovery upsize could both fire, and a third contract
could then rest behind them. Every order now asks the same object how many
contracts it may buy and whether the money is there.

THE DAY IS NEW YORK, AND THAT IS THE EXCHANGE'S OWN DEFINITION. Kalshi's API
documents its utilisation caps resetting "at midnight New York time", so a
system that reconciles on UTC is keeping books on a different day from the
venue it trades on. DST is handled by the tz database rather than by a rule of
thumb - a hand-rolled offset is wrong twice a year, quietly, for an hour, and
an hour of misfiled trades at a day boundary is exactly where a loss floor
gets the wrong window.

GROWTH IS REVIEWED, NOT CONTINUOUS. The base tier changes only at the daily
reconciliation, from RECONCILED CAPITAL - settled cash, excluding unrealised
gains on open positions. Sizing up on an open position's mark is sizing up on
money that can still evaporate, and it compounds exposure exactly when a
position is winning and most likely to be given back.

RECOVERY IS SEPARATE AND DOES NOT GROW THE TIER. The deficit buys at most the
one conditional add; it never raises the base. Once the deficit clears, unfilled
recovery adds are cancelled and sizing returns to base immediately.
"""

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def ny_day(now_ms: int) -> str:
    """The New York calendar day, as YYYY-MM-DD."""
    return dt.datetime.fromtimestamp(now_ms / 1000, tz=dt.UTC).astimezone(NY).strftime(
        "%Y-%m-%d"
    )


def ny_day_start_ms(now_ms: int) -> int:
    """Midnight America/New_York for the day `now_ms` falls in, in epoch ms.

    Built by localising midnight rather than by subtracting a fixed offset, so
    the spring-forward and fall-back days are the right length.
    """
    local = dt.datetime.fromtimestamp(now_ms / 1000, tz=dt.UTC).astimezone(NY)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


@dataclass(frozen=True)
class Capital:
    """What the account is, as of the last reconciliation."""

    ny_day: str
    reconciled_cash: float       # settled cash at the review
    open_exposure: float         # positions and resting orders, NOT counted as capital
    base_contracts: int
    account_ceiling: float
    reconciled_ms: int

    @property
    def capital_for_sizing(self) -> float:
        """Settled cash only. Unrealised gains are deliberately excluded."""
        return self.reconciled_cash


def tier_for(capital: float, per_contract: float, ceiling: int) -> int:
    """Contracts the reconciled capital supports. At least one, never above
    the ceiling.

    A step function on purpose: it changes once a day, at the review, so the
    size a trade goes out at is knowable in advance rather than a function of
    whatever the balance happened to be that second.
    """
    if per_contract <= 0:
        return 1
    return max(1, min(ceiling, int(capital // per_contract)))


class CapitalController:
    """The single sizing authority. Reads the broker; never guesses."""

    def __init__(self, settings, store) -> None:
        self._settings = settings
        self._store = store

    # ------------------------------------------------------- reconciliation

    async def reconcile(self, trader, now_ms: int, force: bool = False) -> Capital | None:
        """Establish the day's capital from the exchange. Never raises.

        Runs at startup and at the first poll after midnight New York. Returns
        None when the broker could not be read - the caller keeps yesterday's
        tier rather than inventing one, because an unreadable balance is not a
        zero balance and it is certainly not a bigger one.
        """
        try:
            day = ny_day(now_ms)
            existing = self._store.capital_for_day(day)
            if existing is not None and not force:
                return existing
            if trader is None:
                return existing
            cash = await trader.balance_dollars()
            if cash < 0:
                return existing
            _orders, resting = await trader.resting_exposure()
            held_cost = self._store.open_position_cost()
            exposure = round(max(0.0, resting) + max(0.0, held_cost), 6)
            # AVAILABLE CASH IS NOT CAPITAL. An open position and a resting
            # order both reduce spendable cash without reducing what the
            # account is worth - the money is committed, not gone. Sizing off
            # cash alone would shrink the tier every time a trade was on, and
            # grow it again the moment one settled, which is a sizing rule
            # driven by whether we happen to be in a position.
            #
            # Capital is settled cash PLUS the COST BASIS of what is
            # committed. Cost, never the mark: unrealised gains are excluded,
            # so a winning open position cannot raise tomorrow's size on money
            # that has not arrived.
            capital_for_tier = round(cash + exposure, 6)
            tier = tier_for(
                capital_for_tier,
                self._settings.capital_per_contract,
                self._settings.max_base_contracts,
            )
            capital = Capital(
                ny_day=day, reconciled_cash=capital_for_tier,
                open_exposure=exposure, base_contracts=tier,
                account_ceiling=self._settings.recovery_add_test_budget,
                reconciled_ms=now_ms,
            )
            self._store.record_capital_day(capital)
            previous = self._store.previous_capital_day(day)
            if previous and previous.base_contracts != tier:
                print(
                    f"capital review [{day}]: cash {cash:.2f} -> base tier "
                    f"{previous.base_contracts} -> {tier}",
                    flush=True,
                )
            return capital
        except Exception as exc:  # noqa: BLE001 - sizing must never stop trading
            print(f"capital reconcile failed: {exc!r}", flush=True)
            return None

    # -------------------------------------------------------------- sizing

    def base_contracts(self, now_ms: int) -> int:
        """The reviewed tier. One until a review says otherwise."""
        capital = self._store.capital_for_day(ny_day(now_ms))
        return capital.base_contracts if capital else 1

    # --------------------------------------------------------------- funds

    async def available(self, trader, now_ms: int) -> float:
        """Spendable dollars, checked fresh. Negative when unknown.

        BROKER CASH IS ALREADY NET OF RESTING ORDERS. Kalshi documents resting
        and pending orders as reserving "their full worst-case cost plus
        fees", and `balance` is the AVAILABLE balance. Subtracting resting
        exposure from it again would deduct the same dollars twice and refuse
        orders the account could afford.

        The account ceiling is a SEPARATE constraint measuring a different
        thing - how big the test is allowed to get - so it subtracts committed
        exposure from the ceiling, not from the cash. The binding limit is
        whichever is smaller.

        Local reservations ARE subtracted, because they are orders this process
        has decided on but the broker has not seen yet.
        """
        if trader is None:
            return -1.0
        cash = await trader.balance_dollars()
        if cash < 0:
            return -1.0
        _orders, resting = await trader.resting_exposure()
        if resting < 0:
            return -1.0
        held = self._store.get_setting("open_mark", 0.0)
        exposure = round(max(0.0, resting) + max(0.0, held), 6)
        ceiling_room = self._settings.recovery_add_test_budget - exposure
        reserved = self._store.reserved_funds(now_ms)
        return round(min(cash, ceiling_room) - reserved, 6)

    async def reserve_checked(
        self, trader, key: str, amount: float, now_ms: int
    ) -> bool:
        """Read available funds and claim them in one go.

        The read and the claim have to be adjacent: two orders that each read
        the balance and then each reserve against it will both succeed on a
        stale figure. `Store.reserve_funds` re-checks the live total of
        reservations inside its own transaction, so the loser is refused.
        """
        available = await self.available(trader, now_ms)
        if available < 0:
            return False
        return self._store.reserve_funds(key, amount, now_ms, available=available)

    def reserve(self, key: str, amount: float, now_ms: int) -> bool:
        """Claim funds for an order about to be sent. Atomic; False if taken.

        Without this, a base entry and a recovery add evaluated in the same
        poll both read the same balance and both believe they can afford it.
        The reservation includes the fee, because Kalshi reserves worst-case
        cost PLUS fees and a reservation that is smaller than the real charge
        is not a reservation.
        """
        return self._store.reserve_funds(key, amount, now_ms)

    def release(self, key: str) -> None:
        self._store.release_funds(key)
