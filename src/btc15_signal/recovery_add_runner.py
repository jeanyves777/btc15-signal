"""Drives the conditional recovery add-on against the live book.

`recovery_add.py` decides; this places, watches, cancels and reconciles. The
split is deliberate - every judgement in the decision module is a pure function
and is unit-tested without a broker, so the only thing that needs a live
account is the part that cannot be reasoned about: races.

THE RACES THIS IS BUILT AROUND

* **Cancel loses to a fill.** A cancel is sent, the order fills first, Kalshi
  answers 404. That is not an error - it is a position we now hold. Every
  cancel therefore re-reads the order and banks a fill if it finds one.
* **A response is lost.** The order may or may not exist. The
  `client_order_id` is a pure function of the position, so a retry is refused
  by Kalshi and by the UNIQUE column rather than opening a second contract.
* **A restart mid-flight.** `adds_needing_reconciliation` finds every add that
  was PENDING with an order id and asks the exchange what became of it, before
  any new decision is taken.
* **A fill reported twice.** `record_add_fill` is guarded on `filled_count =
  0`, so the lifetime budget is charged once.

BOUNDED EXPOSURE. The ceiling is checked against lifetime fills PLUS whatever
is resting at the broker right now, and an exposure that cannot be read counts
as no room. The budget never decreases, so a loss or a restart cannot hand the
test another $30.

SHADOW BY DEFAULT. `recovery_add_enabled` is False, and with it off everything
here still runs and records - the decision, the reason, the price it would have
rested at - and places nothing. That is the evidence that decides whether it
goes live, and it is collected the same way whether or not orders are real.
"""

import json
import traceback

from .capital import CapitalController
from .config import Settings
from .execution import parse_fill
from .recovery_add import (
    AddLimits,
    AddState,
    client_order_id,
    evaluate,
    should_cancel,
)
from .validation import kalshi_fee_charged


def limits_from(settings: Settings) -> AddLimits:
    return AddLimits(
        max_total_funding=settings.recovery_add_test_budget,
        max_contracts_per_position=settings.recovery_add_max_contracts,
        dip_cents=settings.recovery_add_dip,
        min_seconds_remaining=settings.recovery_add_min_seconds,
        distance_floor=settings.recovery_add_distance_floor,
    )


class RecoveryAddRunner:
    """One per service. Never raises into the trading loop."""

    def __init__(self, settings: Settings, store, telegram=None) -> None:
        self._settings = settings
        self._store = store
        self._telegram = telegram
        self._limits = limits_from(settings)
        self._reconciled = False

    @property
    def live(self) -> bool:
        return bool(self._settings.recovery_add_enabled)

    async def step(
        self, *, trader, contract, features, crossed, remaining_s: int,
        now_ms: int, opened: int,
    ) -> None:
        """One poll. Swallows its own errors, like every research path here."""
        try:
            await self._step(
                trader=trader, contract=contract, features=features,
                crossed=crossed, remaining_s=remaining_s, now_ms=now_ms,
                opened=opened,
            )
        except Exception as exc:  # noqa: BLE001 - must never stop trading
            print(f"recovery add-on failed: {exc!r}", flush=True)
            if self._settings.reference_debug:
                traceback.print_exc()

    async def _step(
        self, *, trader, contract, features, crossed, remaining_s, now_ms, opened,
    ) -> None:
        if trader is not None and not self._reconciled:
            await self.reconcile(trader, now_ms)
            self._reconciled = True

        existing = self._store.open_add(opened)
        if existing is not None and existing["state"] == AddState.PENDING:
            await self._maintain(
                trader, existing, features, crossed, remaining_s, now_ms, opened
            )
            return
        if existing is not None and existing["state"] != AddState.DEFERRED:
            return  # already executed, skipped or cancelled - one per position

        position = self._store.open_position_detail(opened)
        if position is None:
            return
        side, paid, _count, ticker, _proposal = position
        await self._consider(
            trader, contract, ticker, side, paid, features, crossed,
            remaining_s, now_ms, opened,
        )

    # ------------------------------------------------------------- decide

    async def _consider(
        self, trader, contract, ticker, side, paid, features, crossed,
        remaining_s, now_ms, opened,
    ) -> None:
        state = self._store.recovery_state(self._settings.recovery_steps)
        # FUNDS ARE CHECKED FRESH, EVERY ORDER, against the testing account.
        #
        # Not a lifetime spend total: money that came back is available again,
        # and a cap that counts it would stop recovery for lack of a number
        # rather than lack of funds. What gates the order is cash on hand and
        # exposure already committed, read now. Either being unreadable yields
        # no room - an unknown account is not an empty one, but it is not a
        # licence to spend either.
        resting = 0.0
        balance = -1.0
        if trader is not None:
            _orders, resting = await trader.resting_exposure()
            balance = await trader.balance_dollars()
        held = self._store.get_setting("open_mark", 0.0)
        exposure = (
            -1.0 if resting < 0 else round(max(0.0, resting) + max(0.0, held), 6)
        )
        room = self._store.account_room(
            self._settings.recovery_add_test_budget, balance, exposure
        )
        committed, _fills = self._store.add_budget_committed()
        # `evaluate` reasons in "already committed against the ceiling", so the
        # account ceiling minus the room available expresses the same gate.
        spent = -1.0 if room < 0 else round(
            self._settings.recovery_add_test_budget - room, 6
        )

        current_ask = None
        if contract is not None:
            try:
                current_ask = contract.ask(side)
            except (AttributeError, ValueError):
                current_ask = None

        def judge(crossed_since_entry: bool):
            return evaluate(
                features=features,
                entry_side=side,
                entry_fill=paid,
                current_ask=current_ask,
                crossed_since_entry=crossed_since_entry,
                remaining_s=remaining_s,
                required_per_trade=state.required_per_trade(),
                recovery_active=state.active,
                already_added=False,
                open_exposure=spent,
                limits=self._limits,
                fee=kalshi_fee_charged,
            )

        # UNKNOWN IS STILL NOT A PASS. When the crossing cannot be established
        # the conservative assumption stands - we evaluate as though it DID
        # cross, which vetoes - and nothing below ever acts on the other
        # branch. What the second call decides is only whether that veto is
        # TERMINAL, and that distinction is the whole defect:
        #
        # the position gets ONE evaluation, and on 2026-09-23 all 25 refusals
        # were spent on "crossing history unavailable" - every one of them
        # because BRTI's series trailed the entry instant by 0-2 seconds, and
        # every one answerable a second later. A feed that is briefly behind
        # is not a decision about this market.
        #
        # So: if the ONLY thing in the way is the unanswerable question, the
        # row is DEFERRED and the next poll asks again. If the add would have
        # been refused anyway, that refusal is real, is independent of the
        # crossing, and is recorded under its own reason rather than mislabelled
        # as a data problem.
        deferred = False
        decision = judge(bool(crossed) if crossed is not None else True)
        if crossed is None:
            without_crossing = judge(False)
            if without_crossing.place:
                deferred = True
                decision = type(decision)(
                    False,
                    "BRTI has no sample covering the entry instant yet; "
                    "will re-ask next poll",
                    without_crossing.price, decision.failed,
                )
            else:
                decision = without_crossing

        conditions = json.dumps({
            "side": getattr(features, "side", None),
            "distance": getattr(features, "brti_normalized_distance", None),
            "momentum": getattr(features, "brti_momentum_bps", None),
            "stale": getattr(features, "stale", None),
            "crossed": crossed,
            "remaining_s": remaining_s,
            "committed": committed,
            "resting": resting,
        })
        coid = client_order_id(ticker, side, opened)
        base = {
            "client_order_id": coid,
            "window_open_ms": opened,
            "ticker": ticker,
            "side": side,
            "base_fill": paid,
            "limit_price": decision.price,
            "count": self._settings.recovery_add_max_contracts,
            "deficit_at_placement": state.deficit,
            "required_at_placement": state.required_per_trade(),
            "conditions_at_placement": conditions,
            "updated_ms": now_ms,
        }

        if not decision.place:
            # DEFERRED is written the same way and read differently: it is the
            # one state `_step` will come back to, so the question gets asked
            # again while the window is still open. It becomes terminal by
            # itself - the add places, or a real refusal replaces it, or the
            # add deadline passes and `evaluate` refuses on the clock.
            base["state"] = AddState.DEFERRED if deferred else AddState.SKIPPED
            base["cancel_reason"] = decision.reason
            self._store.record_or_advance_add(base)
            # The state strings are "RECOVERY ADD SKIPPED"/"...DEFERRED", so
            # print the last word - "recovery add RECOVERY ADD SKIPPED" is how
            # the first deploy of this read in the operator's log.
            print(
                f"recovery add {str(base['state']).rsplit(' ', 1)[-1]} "
                f"[{ticker}]: {decision.reason}",
                flush=True,
            )
            return

        # RESERVE BEFORE SENDING, including the fee. Kalshi reserves worst-case
        # cost plus fees, so a local claim that omits the fee is smaller than
        # the money actually committed.
        cost = decision.price * self._settings.recovery_add_max_contracts
        claim = round(
            cost + kalshi_fee_charged(
                decision.price, self._settings.recovery_add_max_contracts
            ),
            6,
        )
        if self.live and trader is not None:
            capital = CapitalController(self._settings, self._store)
            if not await capital.reserve_checked(
                trader, f"add:{opened}", claim, now_ms
            ):
                base["state"] = AddState.SKIPPED
                base["cancel_reason"] = (
                    f"could not reserve {claim:.4f} against available funds"
                )
                self._store.record_or_advance_add(base)
                return
        expiration = max(
            (contract.close_ms // 1000) - self._limits.min_seconds_remaining,
            now_ms // 1000 + 5,
        )
        base["state"] = AddState.PENDING
        base["placed_ms"] = now_ms
        base["expiration_ts"] = expiration
        # STILL THE PLACEMENT GUARD. `record_or_advance_add` advances a row
        # only while it is DEFERRED - our own open question - and refuses every
        # terminal state exactly as the bare insert did, so no second order can
        # be opened against a position that already has one.
        if not self._store.record_or_advance_add(base):
            return  # another pass already owns this position

        if not self.live or trader is None:
            self._store.update_add(coid, {
                "state": AddState.SKIPPED,
                "cancel_reason": "shadow mode - not placed",
                "updated_ms": now_ms,
            })
            print(
                f"recovery add SHADOW [{ticker}]: would rest "
                f"{self._settings.recovery_add_max_contracts} at "
                f"{decision.price:.2f} - {decision.reason}",
                flush=True,
            )
            return

        try:
            order = await trader.place_resting_buy(
                ticker=ticker, side=side, price=decision.price,
                count=self._settings.recovery_add_max_contracts,
                expiration_ts=expiration, client_order_id=coid,
            )
        except Exception as exc:  # noqa: BLE001
            # The order may or may not exist. The deterministic id means the
            # next pass cannot double it, so record and let reconcile settle it.
            # THE RESERVATION STAYS. The order may or may not exist, so the
            # money may or may not be committed; releasing it here would let
            # the next order spend funds an in-flight one might already hold.
            # `reconcile` resolves it against the broker.
            self._store.update_add(coid, {
                "cancel_reason": f"placement failed: {type(exc).__name__}: {exc}"[:200],
                "updated_ms": now_ms,
            })
            print(f"recovery add placement failed [{ticker}]: {exc!r}", flush=True)
            return

        order_id = (order.get("order") or order).get("order_id")
        self._store.update_add(coid, {
            "order_id": order_id,
            "updated_ms": now_ms,
        })
        # THE HANDOFF. The broker now reserves this order's cost itself, and
        # its `balance` is already net of it. Keeping the local reservation as
        # well would subtract the same dollars twice and refuse the next order
        # money the account actually has. Released only on a CONFIRMED
        # placement - an unknown outcome keeps it held until reconciliation
        # says what happened.
        if order_id:
            self._store.release_funds(
                f"add:{opened}", reason="broker holds the reservation"
            )
        print(
            f"recovery add PENDING [{ticker}] {decision.price:.2f} - "
            f"{decision.reason}",
            flush=True,
        )

    # ----------------------------------------------------------- maintain

    async def _maintain(
        self, trader, existing, features, crossed, remaining_s, now_ms, opened
    ) -> None:
        position = self._store.open_position_detail(opened)
        state = self._store.recovery_state(self._settings.recovery_steps)

        # A fill found here ends the lifecycle, so it is checked before any
        # cancellation is considered.
        if (
            trader is not None
            and existing["order_id"]
            and await self._bank_if_filled(
                trader, existing, now_ms, crossed, features
            )
        ):
            return

        # UNKNOWN IS NOT "CROSSED". On the place path an unanswerable crossing
        # question is an explicit refusal; here it is a reason to pull the
        # order, because a resting bid whose justification cannot be checked is
        # not a bid anyone chose to have. The two are different decisions and
        # the reason recorded says which.
        cancel, reason = should_cancel(
            features=features,
            entry_side=existing["side"],
            crossed_since_entry=bool(crossed),
            remaining_s=remaining_s,
            recovery_active=state.active,
            recovery_owes=state.owes,
            base_position_open=position is not None,
            limits=self._limits,
        )
        if not cancel and crossed is None:
            cancel, reason = True, "crossing history unavailable; cannot verify"
        if not cancel:
            return
        await self._cancel(trader, existing, reason, now_ms, crossed, features)

    async def _bank_if_filled(
        self, trader, existing, now_ms, crossed, features
    ) -> bool:
        order = await trader.order_status(existing["order_id"])
        # ONE PARSER, on the broker's real field names. This read
        # `taker_fill_count`/`maker_fill_count`/`filled_count` and
        # `average_fill_price_dollars`, none of which Kalshi returns - and
        # fell back to `yes_price_dollars`, which on a DOWN position is the
        # COMPLEMENT of the price paid. It would have recorded 0.17 for a fill
        # at 0.83.
        detail = parse_fill(order, existing["side"])
        if detail is None:
            return False
        filled, price = detail["count"], detail["price"]
        fee = detail["fee"]
        self._store.record_add_fill(
            existing["client_order_id"], filled, price, fee, now_ms,
            is_taker=detail["is_taker"],
            conditions=json.dumps({
                "side": getattr(features, "side", None),
                "distance": getattr(features, "brti_normalized_distance", None),
                "momentum": getattr(features, "brti_momentum_bps", None),
                "crossed": crossed,
            }),
        )
        # Filled: the money is position exposure now, not a pending claim.
        self._store.release_funds(
            f"add:{existing['window_open_ms']}", reason="filled"
        )
        print(
            f"recovery add EXECUTED [{existing['ticker']}] {filled:g} at "
            f"{price:.4f}, fee {fee:.4f}, {'maker' if maker else 'taker'}",
            flush=True,
        )
        return True

    async def _cancel(
        self, trader, existing, reason, now_ms, crossed=None, features=None
    ) -> None:
        if trader is None or not existing["order_id"]:
            self._store.update_add(existing["client_order_id"], {
                "state": AddState.CANCELLED, "cancel_reason": reason,
                "cancelled_ms": now_ms, "updated_ms": now_ms,
            })
            return
        cancelled, note = await trader.cancel_order(existing["order_id"])
        # THE RACE. A cancel that failed may mean the order filled first, so
        # ask before recording a cancellation that did not happen - otherwise
        # a contract we now hold is written down as never placed.
        if not cancelled and await self._bank_if_filled(
            trader, existing, now_ms, crossed, features
        ):
            return
        self._store.update_add(existing["client_order_id"], {
            "state": AddState.CANCELLED,
            "cancel_reason": f"{reason} ({note})",
            "cancelled_ms": now_ms,
            "updated_ms": now_ms,
        })
        self._store.release_funds(
            f"add:{existing['window_open_ms']}", reason="cancel confirmed"
        )
        print(
            f"recovery add CANCELLED [{existing['ticker']}]: {reason} ({note})",
            flush=True,
        )

    # --------------------------------------------------------- reconcile

    async def reconcile(self, trader, now_ms: int) -> None:
        """After a restart, ask the exchange what happened to anything pending."""
        for existing in self._store.adds_needing_reconciliation():
            try:
                if await self._bank_if_filled(trader, existing, now_ms, None, None):
                    continue
                order = await trader.order_status(existing["order_id"])
                status = (order or {}).get("status")
                if status in (None, "canceled", "cancelled", "expired"):
                    self._store.update_add(existing["client_order_id"], {
                        "state": AddState.CANCELLED,
                        "cancel_reason": f"reconciled after restart ({status})",
                        "cancelled_ms": now_ms, "updated_ms": now_ms,
                    })
            except Exception as exc:  # noqa: BLE001
                print(f"recovery add reconcile failed: {exc!r}", flush=True)
