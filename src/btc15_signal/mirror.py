"""Copy every execution to one or two other Kalshi accounts.

WHAT THIS IS. A wrapper around `KalshiExecutionClient` that forwards the three
order-placing calls - entry, upsize (recovery add), exit - to additional
accounts, each with its own API key AND ITS OWN SIZING. Everything else
(balance, fills, settlements, positions, order status) is delegated to the
PRIMARY account only, because the mirrors are deliberately not tracked: they are
a copy destination, not a second book.

WHY A WRAPPER. All five order call sites in this codebase reach the broker
through one object built once in `main.py`, and `recovery_add_runner` receives
that same object as a parameter. Wrapping it at construction covers every path
without touching any call site, so a mirror can never change the primary's
pricing, sizing or gating.

SIZING IS PER ACCOUNT, BASE THROUGH UPSIZE. A mirror does not inherit the
primary's count and is not a multiple of it. Each account declares, in the same
units the operator already uses:

  * `base_budget`    dollars PER CONTRACT, the same meaning as `auto_budget`
  * `base_contracts` how many contracts at base - the tier
  * `add_contracts`  the upsize, i.e. what a recovery add buys on this account
  * `max_contracts`  a hard ceiling applied after everything else

and the count is computed with `contracts_for_budget`, the primary's own
rounding rule (whole contracts, round down, floor of one). A mirror on a smaller
account therefore stays small when the primary's growth controller raises the
primary's tier, and an account meant to run bigger can do so without touching
the strategy. Sizing stays the operator's, per account.

THE RULES, in order of how badly each one bites if broken:

1. THE PRIMARY IS NEVER BLOCKED AND NEVER FAILS BECAUSE OF A MIRROR. Forwarding
   happens after the primary's call has returned, on a queue drained by a
   background worker. A mirror that is down, rate-limited, out of money or
   misconfigured produces a log line and nothing else.

2. AN ENTRY IS MIRRORED ONLY IF THE PRIMARY ACTUALLY GOT CONTRACTS. The primary
   entry is immediate-or-cancel and misses outright a fraction of the time
   (FINDINGS 22). Mirroring an intent rather than a fill would open positions on
   the other accounts that this account does not have, and nothing downstream
   would ever close them - the exit only fires for positions the primary holds.

3. ORDER IS PRESERVED PER MIRROR. One worker per mirror, one queue, strictly
   sequential. Two accounts run concurrently with each other, but within an
   account an exit can never overtake the entry it is exiting.

4. A MIRROR EXITS WHAT IT BOUGHT, NOT WHAT THE PRIMARY BOUGHT. Because sizing
   is independent the two counts differ, so each mirror remembers the size it
   sent per (ticker, side) and closes that. Exits are reduce-only already, which
   is what makes this safe: over-sending cannot open an opposite position, and a
   mirror whose entry never filled simply does nothing on exit.

WHAT IS DELIBERATELY NOT SOLVED. Mirror fills will differ from the primary's.
The forwarded order goes out tens to hundreds of milliseconds later, at the same
limit, into a book that may have moved; the primary's limit is a ceiling it
crosses to (see `execute_with_take_profit`), so a mirror pays whatever real ask
is there inside that ceiling, or misses. Mirror P&L is therefore NOT the
primary's P&L and no code here pretends otherwise. That is the accepted cost of
not tracking them.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .execution import ExecutionResult, KalshiExecutionClient
from .validation import contracts_for_budget

# Statuses from `execute_with_take_profit` that mean contracts were actually
# bought: 'filled' is a plain buy, 'protected' has a take-profit resting behind
# it, 'unprotected' means the entry filled and the take-profit did not. All
# three are real positions and all three must be mirrored. Kept in step with
# store.HELD_STATUSES, restated here to avoid an import cycle.
FILLED_STATUSES = ("filled", "protected", "unprotected")


@dataclass(frozen=True)
class MirrorTarget:
    """One destination account, with sizing that is entirely its own."""

    name: str
    api_key_id: str
    private_key_path: str
    # Base sizing. `base_budget` is dollars PER CONTRACT; 0 means "ignore the
    # budget and buy `base_contracts` outright", which is the simple case.
    base_budget: float = 0.0
    base_contracts: int = 1
    # The upsize. 0 means "use the same count the primary's add used", for an
    # account that should track the primary's upsize rather than set its own.
    add_contracts: int = 0
    # Hard ceiling, applied last to every order on this account. 0 = none.
    max_contracts: int = 0
    # FUND THE MARKET'S SHARD BEFORE EACH ORDER, as the Kalshi app does for a
    # manual trade. ON for mirrors: on 2026-09-26 the operator's wife's mirror
    # spent shard 2 to $0.09 and was refused every entry for ~15 hours with $30
    # in shard 0 - money the app would have used without a word. See
    # `KalshiExecutionClient.ensure_funds`.
    auto_fund: bool = True
    fund_source_shard: int = 0
    # THE RECOVERY SIZE, in dollars, for an entry the primary's loss step
    # upsized. Operator, 2026-09-27: "make sure the recovery size is set to $2
    # as well" - the same budget the primary's `loss_step_budget` uses. 0 means
    # a recovery is sized like any other entry on this account.
    recovery_budget: float = 0.0
    # THE ALL-SIGNAL $1 STRATEGY on this account: dollars per signal
    # (operator, 2026-09-28). Keyed on the order's strategy, never on shared
    # state, so a main-strategy entry in flight at the same moment cannot be
    # sized by it.
    allsignal_budget: float = 1.0

    def recovery_count(self, price: float) -> int:
        """Contracts for a recovery entry: this account's own dollar budget at
        the real price, then the account's hard ceiling."""
        return self._cap(contracts_for_budget(self.recovery_budget, price))

    def _cap(self, n: int) -> int:
        n = max(1, int(n))
        if self.max_contracts > 0:
            n = min(n, self.max_contracts)
        return n

    def entry_count(self, price: float) -> int:
        """Contracts for a base entry at `price`, this account's own size.

        Mirrors `main.py`'s own formula: a per-contract budget times the tier,
        converted at the real price, then never allowed to exceed the tier. The
        budget can only make an order SMALLER than the tier, never larger.
        """
        tier = max(1, int(self.base_contracts))
        if self.base_budget <= 0:
            return self._cap(tier)
        count = contracts_for_budget(self.base_budget * tier, price)
        return self._cap(min(count, tier))

    def add_count(self, primary_count: float) -> int:
        if self.add_contracts > 0:
            return self._cap(self.add_contracts)
        return self._cap(primary_count)


def _with_funding(note: str, client) -> str:
    """Append what funding did to a log note, so a transfer is never silent."""
    funding = getattr(client, "last_funding_note", "") or ""
    client.last_funding_note = ""
    if not funding:
        return note
    return f"{note} | funding: {funding}" if note else f"funding: {funding}"


class _Mirror:
    """A destination account plus the single worker that keeps its order."""

    def __init__(self, target: MirrorTarget, base_url: str, log) -> None:
        self.target = target
        self.client = KalshiExecutionClient(
            base_url, target.api_key_id, target.private_key_path
        )
        self.client.auto_fund = target.auto_fund
        self.client.fund_source_shard = target.fund_source_shard
        self._log = log
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        # What THIS account was told to buy, per (ticker, side), so its exit can
        # close its own size rather than the primary's. In memory only: after a
        # restart it is empty and an exit falls back to the configured ceiling,
        # which reduce-only clamps to whatever is actually held.
        self.held: dict[tuple[str, str], int] = {}
        # primary order id -> this account's order id, so a cancel can be
        # forwarded. Also in memory only; `place_resting_buy` carries an
        # `expiration_ts` and the broker expires the order itself, so a lost
        # mapping leaves an orphan that is time-bounded, not permanent.
        self.order_ids: dict[str, str] = {}

    def start(self) -> None:
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job is None:
                    return
                kind, args = job
                try:
                    # A combo waits for a maker's quote, so it gets that wait
                    # plus room to confirm and read the fill.
                    # 25 s: an entry may top up and resend once (2026-09-30).
                    limit = (25.0 if kind != "combo"
                             else float(args.get("wait_s", 25.0)) + 25.0)
                    await asyncio.wait_for(self._apply(kind, args), timeout=limit)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - never raise at a mirror
                    # str(exc) now carries Kalshi's reason (see
                    # KalshiExecutionClient._post), and the funding note says
                    # whether a top-up was tried - together they tell a
                    # balance problem from a malformed order at a glance.
                    self._log(self.target.name, kind, "error", args,
                              _with_funding(str(exc), self.client))
                    self._answer(args, "error")
            finally:
                self.queue.task_done()

    def _answer(self, args: dict, status: str, filled: float = 0.0, count: int = 0,
                stake=None) -> None:
        """Tell a dispatcher waiting on this copy - the copy path once the primary
        is done (2026-10-05, FINDINGS 163) - what it did. Never raises."""
        try:
            fut = (args.get("answers") or {}).get(self.target.name)
            if fut is not None and not fut.done():
                fut.set_result({"status": status, "filled": float(filled or 0),
                                "count": int(count or 0), "stake": stake})
        except Exception:  # noqa: BLE001
            pass

    async def _apply(self, kind: str, args: dict) -> None:
        if kind == "entry":
            proposal = args["proposal"]
            stake = None
            if getattr(proposal, "strategy", "") == "allsignal":
                # THIS COPY'S STAKE, decided at the dispatch (after a loss, past
                # a target - main.mirror_stake_now, 2026-10-05); else the day's.
                stake = float(args.get("stake") or self.target.allsignal_budget)
                order_limit = float(
                    args.get("ceiling")
                    or min(0.99, proposal.entry_limit + max(0.0, args.get("slippage", 0)))
                )
                guard = getattr(self.client, "daily_profit_guard", None)
                dynamic = guard.entry_count(order_limit) if guard is not None else None
                count = (int(dynamic) if dynamic is not None
                         else self.target._cap(contracts_for_budget(stake, proposal.entry_limit)))
                if count <= 0:
                    self._answer(args, "paused", 0, 0, stake)
                    self._log(self.target.name, kind, "paused",
                              {"ticker": proposal.ticker, "side": proposal.side,
                               "count": 0, "limit": order_limit,
                               "primary_count": args["filled_count"]},
                              "daily capital risk cap cannot fund one contract")
                    return
            elif args.get("recovery") and self.target.recovery_budget > 0:
                count = self.target.recovery_count(proposal.entry_limit)
            else:
                count = self.target.entry_count(proposal.entry_limit)
            result = await self.client.execute_with_take_profit(
                replace(proposal, count=count), args["slippage"], args["ceiling"]
            )
            if result.status in FILLED_STATUSES and result.filled_count > 0:
                key = (proposal.ticker, proposal.side)
                self.held[key] = self.held.get(key, 0) + int(result.filled_count)
            self._answer(args, result.status,
                         result.filled_count if result.status in FILLED_STATUSES else 0,
                         count, stake)
            self._log(
                self.target.name, kind, result.status,
                {"ticker": proposal.ticker, "side": proposal.side,
                 "count": count, "limit": proposal.entry_limit,
                 "primary_count": args["filled_count"]},
                _with_funding(result.note, self.client),
            )
        elif kind == "exit":
            key = (args["ticker"], args["side"])
            count = self.held.get(key)
            fallback = count is None
            if fallback:
                # Unknown - the process restarted while a position was open.
                # Send the largest size this account could be holding and let
                # reduce-only clamp it down to the truth.
                count = self.target.max_contracts or self.target.base_contracts
                inventory = getattr(self.client, "held_contracts", None)
                if inventory is not None:
                    # Budgets can now buy more than the old two-contract cap.
                    # Recover THIS account's size, rather than guessing from
                    # today's budget after a restart or a size change.
                    count = await inventory(args["ticker"], args["side"])
                    if count <= 0:
                        self._log(self.target.name, kind, "not-held",
                                  {"ticker": args["ticker"]}, "No position to close")
                        return
            result = await self.client.close_position(
                args["ticker"], args["side"], max(1, int(count)),
                args["limit_price"], args["floor"],
            )
            if result.status not in ("error",) and not fallback:
                self.held.pop(key, None)
            self._log(
                self.target.name, kind, result.status,
                {"ticker": args["ticker"], "side": args["side"],
                 "count": max(1, int(count)), "limit": args["limit_price"],
                 "from": "fallback" if fallback else "held",
                 "primary_count": args["count"]},
                result.note,
            )
        elif kind == "add":
            count = self.target.add_count(args["count"])
            # The client order id stays DETERMINISTIC - that is what stops a
            # retry or a restart opening a second contract - but is namespaced
            # per mirror so two accounts driven by one process cannot collide.
            coid = f"{args['client_order_id']}-{self.target.name}"
            order = await self.client.place_resting_buy(
                args["ticker"], args["side"], args["price"], count,
                args["expiration_ts"], coid,
            )
            oid = (order or {}).get("order_id") or (order or {}).get("id")
            if oid and args.get("primary_order_id"):
                self.order_ids[args["primary_order_id"]] = oid
            # A resting add is not held until it fills, and this process is not
            # watching the mirror's fills. Count it as held now: an exit that
            # over-sends is clamped by reduce-only, whereas one that under-sends
            # would leave the upsize stranded to settlement.
            key = (args["ticker"], args["side"])
            self.held[key] = self.held.get(key, 0) + count
            self._log(
                self.target.name, kind, "placed" if oid else "no-id",
                {"ticker": args["ticker"], "side": args["side"], "count": count,
                 "price": args["price"], "client_order_id": coid,
                 "primary_count": args["count"]},
                _with_funding(oid or "", self.client),
            )
        elif kind == "combo":
            # THE RECOVERY COMBO, copied at THIS account's own base size - the
            # entry count its budget buys at the most the combo may cost - with
            # the same price check: nothing above the cheaper leg. Her account asks
            # for its own quote; a maker who answers the primary need not
            # answer her, and then she simply has no trade this window.
            from . import combo_recovery

            count = self.target.entry_count(args["product"])
            result = await combo_recovery.buy(
                self.client, args["legs"], args["market"], count,
                max_ratio=args.get("max_ratio", 1.0),
                wait_s=float(args.get("wait_s", 25.0)),
                # Her account funds the combo shard like every other order.
                fund=bool(self.client.auto_fund),
            )
            self._log(
                self.target.name, kind, result.outcome,
                {"market": args["market"], "count": count,
                 "legs": [f"{l.asset} {l.side} @{l.ask:.2f}" for l in args["legs"]],
                 "limit": args["product"], "price": result.price},
                _with_funding(result.reason, self.client),
            )
        elif kind == "cancel":
            oid = self.order_ids.pop(args["primary_order_id"], None)
            if not oid:
                self._log(
                    self.target.name, kind, "unknown",
                    {"primary_order_id": args["primary_order_id"]},
                    "no mapped order; it expires on its own expiration_ts",
                )
                return
            ok, note = await self.client.cancel_order(oid)
            self._log(self.target.name, kind, "ok" if ok else "failed",
                      {"order_id": oid}, note)

    async def close(self, drain_timeout: float) -> None:
        if self.worker is not None and not self.worker.done():
            await self.queue.put(None)
            try:
                await asyncio.wait_for(self.worker, timeout=drain_timeout)
            except (TimeoutError, asyncio.CancelledError):
                self.worker.cancel()
        await self.client.close()


class MirroringExecutionClient:
    """Delegates everything to `primary`, and copies orders to the mirrors.

    Unknown attributes fall through to the primary account, so this object is a
    drop-in for `KalshiExecutionClient` everywhere in the codebase. Reads are
    never fanned out: `balance_dollars`, `fills`, `settlements`, `open_mark` and
    the rest answer for the primary account alone, which is what keeps the money
    reporting honest - the mirrors are not in the ledger and must not appear in
    it (see the standing rule that P&L comes from the broker for THIS account).
    """

    def __init__(
        self,
        primary: KalshiExecutionClient,
        targets: list[MirrorTarget],
        base_url: str,
        log_path: str = "runtime/mirror.jsonl",
        drain_timeout: float = 10.0,
        gate=None,
    ) -> None:
        self._primary = primary
        # THE PER-ACCOUNT, PER-INSTRUMENT SWITCH: gate(name) -> may this
        # account take NEW positions on this instrument (main.mirror_on). None
        # = always, as before it existed.
        self._gate = gate
        self._log_path = Path(log_path)
        self._drain_timeout = drain_timeout
        self._mirrors: list[_Mirror] = []
        for target in targets:
            try:
                self._mirrors.append(_Mirror(target, base_url, self._write_log))
            except (OSError, ValueError) as exc:
                # A bad key path or an unreadable PEM disables that mirror and
                # nothing else. The primary must still trade.
                self._write_log(target.name, "init", "disabled", {}, str(exc))

    # Set by the service (operator, 2026-10-05; FINDINGS 163): each mirror's $
    # stake per copy - `stake_for(name, proposal)` -> dollars or None (main.
    # mirror_stake_now) - and whether, once the primary is DONE for the day at its
    # cap, its $ signals still go to the mirrors still trading. None/False: as before.
    stake_for = None
    copy_after_primary_done = False
    # {name: label} for what is said ("Affoue"), set by the service; names otherwise.
    labels = None
    # How long the copy path waits for the mirrors' answers (a worker gives up at 25 s).
    COPY_WAIT_S = 30.0

    def __getattr__(self, name):
        return getattr(self._primary, name)

    @property
    def mirror_names(self) -> list[str]:
        return [m.target.name for m in self._mirrors]

    def describe(self) -> str:
        parts = []
        for m in self._mirrors:
            t = m.target
            budget = f"${t.base_budget:.2f}/contract" if t.base_budget > 0 else "no budget"
            # Since 2026-09-27 the recovery is a combo at this account's own
            # base size; nothing on it is upsized.
            recovery = "combo at base size"
            parts.append(
                f"{t.name}: base {t.base_contracts} ({budget}), "
                f"upsize {t.add_contracts or 'as primary'}, "
                f"recovery {recovery}, "
                f"cap {t.max_contracts or 'none'}, "
                f"auto-fund {'on' if t.auto_fund else 'off'}"
            )
        return "; ".join(parts) if parts else "no mirrors"

    def _write_log(self, mirror: str, kind: str, status: str,
                   detail: dict, note: str) -> None:
        row = {"ts": int(time.time() * 1000), "mirror": mirror, "op": kind,
               "status": status, "note": note, **detail}
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError:
            pass  # never let logging break a trade
        print(f"mirror[{mirror}] {kind} {status} {detail} {note}", flush=True)

    # What an account can be switched off from: NEW positions. An exit still
    # goes to an account that holds something this process bought for it, so
    # a position taken before the switch went off is still cashed out/closed.
    ENTRY_KINDS = ("entry", "add", "combo")

    def _copies(self, name: str) -> bool:
        """This account's switch for this instrument. Fails CLOSED: a switch
        that cannot be read copies nothing new."""
        if self._gate is None:
            return True
        try:
            return bool(self._gate(name))
        except Exception as exc:  # noqa: BLE001 - never break the primary
            self._write_log(name, "gate", "error", {}, f"{exc!r} - not copied")
            return False

    def _sized(self, name: str, kind: str, args: dict) -> dict:
        """THIS ACCOUNT'S STAKE for a $ entry copy, decided now (`stake_for`:
        after a loss, past its target - 2026-10-05). The day's stake when there
        is no rule or it cannot tell. Never raises."""
        proposal = args.get("proposal")
        if (kind != "entry" or self.stake_for is None
                or getattr(proposal, "strategy", "") != "allsignal"):
            return args
        try:
            stake = self.stake_for(name, proposal)
        except Exception as exc:  # noqa: BLE001 - never break the primary
            self._write_log(name, "stake", "error", {}, f"{exc!r} - the day's stake")
            return args
        return {**args, "stake": float(stake)} if stake and stake > 0 else args

    def _primary_done(self) -> bool:
        """Is the primary DONE for the day at its daily cap? Never raises: False."""
        guard = getattr(self._primary, "daily_profit_guard", None)
        try:
            return guard is not None and bool(guard.done_for_day())
        except Exception:  # noqa: BLE001
            return False

    def _still_trading(self) -> list[str]:
        """The mirrors whose switch is on and whose OWN day takes a $ entry now
        (below its target, or past it at a lower stake). Fails closed."""
        out = []
        for m in self._mirrors:
            name = m.target.name
            guard = getattr(m.client, "daily_profit_guard", None)
            try:
                if guard is not None and self._copies(name) \
                        and guard.taking_entries("allsignal"):
                    out.append(name)
            except Exception:  # noqa: BLE001
                continue
        return out

    def _dispatch(self, kind: str, args: dict, only=None) -> None:
        for m in self._mirrors:
            name = m.target.name
            if only is not None and name not in only:
                continue
            if kind in self.ENTRY_KINDS and not self._copies(name):
                ticker = (getattr(args.get("proposal"), "ticker", None)
                          or args.get("ticker") or args.get("market"))
                self._write_log(name, kind, "switched-off", {"ticker": ticker},
                                f"mirror_{name}_enabled is off for this instrument")
                continue
            if (kind == "exit" and (args["ticker"], args["side"]) not in m.held
                    and not self._copies(name)):
                # Switched off and holding nothing we bought: do not send a
                # blind reduce-only sale into an account we no longer trade.
                continue
            m.start()
            m.queue.put_nowait((kind, self._sized(name, kind, args)))

    # -- intercepted order paths -----------------------------------------
    async def execute_with_take_profit(
        self, proposal, slippage: float = 0.0, ceiling: float | None = None,
    ) -> ExecutionResult:
        # Read and CLEAR the recovery mark before anything can fail, so it can
        # never leak onto the next, unrelated entry.
        recovery = bool(self.__dict__.pop("entry_is_recovery", False))
        result = await self._primary.execute_with_take_profit(
            proposal, slippage, ceiling
        )
        # RULE 2: only a real fill is mirrored. A missed IOC entry must not
        # become a position on another account.
        if result.status in FILLED_STATUSES and result.filled_count > 0:
            self._dispatch("entry", {
                "proposal": proposal,
                "filled_count": result.filled_count,
                "slippage": slippage,
                "ceiling": ceiling,
                "recovery": recovery,
            })
        elif (result.status == "paused" and self.copy_after_primary_done
              and getattr(proposal, "strategy", "") == "allsignal"
              and self._primary_done()):
            # THE PRIMARY IS DONE FOR THE DAY at its cap; the mirrors are not
            # (operator, 2026-10-05: stop the primary at 8%, Affoue "becomes the
            # account that keeps trading after target hit"; FINDINGS 163). The $
            # signal goes to each mirror still trading by ITS OWN day, and its own
            # order check still decides. Only the cap: a primary blocked for
            # anything else (figures unreadable, BTC only) copies nothing.
            names = self._still_trading()
            if names:
                # EACH COPY ANSWERS BACK (review 2026-10-05): the window is TAKEN
                # only if a mirror bought it; a miss is booked 'unfilled' under
                # COPY_MISSED_ID, so the retry and the chase re-send it through this
                # same path - as for the primary's own miss.
                loop = asyncio.get_running_loop()
                answers = {n: loop.create_future() for n in names}
                self._dispatch("entry", {
                    "proposal": proposal, "filled_count": 0, "slippage": slippage,
                    "ceiling": ceiling, "recovery": False, "answers": answers},
                    only=names)
                await asyncio.wait(list(answers.values()), timeout=self.COPY_WAIT_S)
                said, bought = [], 0.0
                for n in names:
                    fut = answers[n]
                    a = (fut.result() if fut.done() and not fut.cancelled()
                         else {"status": "no answer", "filled": 0.0, "count": 0, "stake": None})
                    label = (self.labels or {}).get(n, n)
                    stake = f" (${a['stake']:g})" if a.get("stake") else ""
                    bought += a["filled"]
                    said.append(f"{label} {a['filled']:g}/{a['count']}{stake}" if a["filled"] > 0
                                else f"{label} {a['status']}{stake}")
                note = COPY_NOTE + "; ".join(said)
                if bought > 0:
                    return ExecutionResult("copied", 0, "", None, note)
                return ExecutionResult("unfilled", 0, COPY_MISSED_ID, None, note)
        return result

    async def close_position(
        self, ticker: str, side: str, count: float, limit_price: float,
        floor: float | None = None,
    ) -> ExecutionResult:
        result = await self._primary.close_position(
            ticker, side, count, limit_price, floor
        )
        # Forwarded whether or not the primary filled. A primary no-fill means
        # nobody bid above the floor on THIS account; the mirror's book may
        # differ, and an unfilled exit there is harmless - immediate-or-cancel
        # and reduce-only, so it simply does nothing.
        self._dispatch("exit", {
            "ticker": ticker, "side": side, "count": count,
            "limit_price": limit_price, "floor": floor,
        })
        return result

    async def place_resting_buy(
        self, ticker: str, side: str, price: float, count: int,
        expiration_ts: int, client_order_id: str,
    ) -> dict:
        order = await self._primary.place_resting_buy(
            ticker, side, price, count, expiration_ts, client_order_id
        )
        primary_id = (order or {}).get("order_id") or (order or {}).get("id")
        self._dispatch("add", {
            "ticker": ticker, "side": side, "price": price, "count": count,
            "expiration_ts": expiration_ts, "client_order_id": client_order_id,
            "primary_order_id": primary_id,
        })
        return order

    def dispatch_combo(self, legs, market: str, product: float,
                       max_ratio: float = 1.0, wait_s: float = 25.0) -> None:
        """Copy a CONFIRMED recovery combo to every mirror (never a maybe)."""
        self._dispatch("combo", {"legs": legs, "market": market,
                                 "product": product, "max_ratio": max_ratio,
                                 "wait_s": wait_s})

    async def cancel_order(self, order_id: str) -> tuple[bool, str]:
        ok, note = await self._primary.cancel_order(order_id)
        self._dispatch("cancel", {"primary_order_id": order_id})
        return ok, note

    async def close(self) -> None:
        # A mirror that will not shut down cleanly must not stop the primary
        # from closing its own connection.
        for m in self._mirrors:
            with contextlib.suppress(Exception):
                await m.close(self._drain_timeout)
        await self._primary.close()


BTC_INSTANCE_ALIASES = ("btc", "primary", "default")


# THE COPY PATH ONCE THE PRIMARY IS DONE (2026-10-05, FINDINGS 163): the note that
# opens a copied window's record, and the order id a missed copy is booked under so
# the retry/chase finds it (main.allsignal_retry_poll needs an order id).
COPY_NOTE = "primary done for the day - copied: "
COPY_MISSED_ID = "mirror-copy"


def current_instance() -> str:
    """The running instance's name, with the original BTC service called `btc`.

    `run_service.py` reads BTC15_INSTANCE to choose its runtime directory and
    leaves it unset for BTC, so an empty value is not "no instance" - it is the
    BTC one.
    """
    return (os.environ.get("BTC15_INSTANCE") or "").strip().lower() or "btc"


def mirror_allowed(settings) -> tuple[bool, str]:
    """May THIS instance forward orders? Returns (allowed, why not).

    Fails closed. All instances share one .env, so an unlisted instance -
    including an instrument added long after this was written - mirrors nothing.
    """
    if not settings.mirror_enabled:
        return False, "MIRROR_ENABLED is false"
    if settings.dry_run:
        return False, "DRY_RUN is true"
    instance = current_instance()
    listed = [
        name.strip().lower()
        for name in (settings.mirror_instances or "").split(",")
        if name.strip()
    ]
    if not listed:
        return False, (
            "MIRROR_INSTANCES is empty - name the instances that may mirror, "
            f"e.g. MIRROR_INSTANCES={instance}"
        )
    allowed = instance in listed or (
        instance == "btc" and any(a in listed for a in BTC_INSTANCE_ALIASES)
    )
    if not allowed:
        return False, (
            f"instance '{instance}' is not in MIRROR_INSTANCES "
            f"({', '.join(listed)})"
        )
    return True, ""


MIRROR_SLOTS = (1, 2, 3)     # m1 Wife, m2 Uncle George, m3 (added 2026-09-30)


def targets_from_settings(settings) -> list[MirrorTarget]:
    """Read up to three mirror accounts out of the settings.

    A target is used only when BOTH its key id and its key path are present, so
    a half-filled block is ignored rather than raising at startup.
    """
    out: list[MirrorTarget] = []
    for idx in MIRROR_SLOTS:
        key_id = (getattr(settings, f"mirror_{idx}_api_key_id", "") or "").strip()
        key_path = (getattr(settings, f"mirror_{idx}_private_key_path", "") or "").strip()
        if not key_id or not key_path:
            continue
        out.append(MirrorTarget(
            name=f"m{idx}",
            api_key_id=key_id,
            private_key_path=key_path,
            base_budget=float(getattr(settings, f"mirror_{idx}_base_budget", 0.0)),
            base_contracts=int(getattr(settings, f"mirror_{idx}_base_contracts", 1)),
            add_contracts=int(getattr(settings, f"mirror_{idx}_add_contracts", 0)),
            max_contracts=int(getattr(settings, f"mirror_{idx}_max_contracts", 0)),
            auto_fund=bool(getattr(settings, f"mirror_{idx}_auto_fund", True)),
            fund_source_shard=int(
                getattr(settings, f"mirror_{idx}_fund_source_shard", 0)),
            recovery_budget=float(
                getattr(settings, f"mirror_{idx}_recovery_budget", 0.0)),
            allsignal_budget=float(
                getattr(settings, f"mirror_{idx}_allsignal_budget", 1.0)),
        ))
    return out
