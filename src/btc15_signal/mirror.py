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
import json
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


class _Mirror:
    """A destination account plus the single worker that keeps its order."""

    def __init__(self, target: MirrorTarget, base_url: str, log) -> None:
        self.target = target
        self.client = KalshiExecutionClient(
            base_url, target.api_key_id, target.private_key_path
        )
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
                    await asyncio.wait_for(self._apply(kind, args), timeout=15.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - never raise at a mirror
                    self._log(self.target.name, kind, "error", args, str(exc))
            finally:
                self.queue.task_done()

    async def _apply(self, kind: str, args: dict) -> None:
        if kind == "entry":
            proposal = args["proposal"]
            count = self.target.entry_count(proposal.entry_limit)
            result = await self.client.execute_with_take_profit(
                replace(proposal, count=count), args["slippage"], args["ceiling"]
            )
            if result.status in FILLED_STATUSES and result.filled_count > 0:
                key = (proposal.ticker, proposal.side)
                self.held[key] = self.held.get(key, 0) + int(result.filled_count)
            self._log(
                self.target.name, kind, result.status,
                {"ticker": proposal.ticker, "side": proposal.side,
                 "count": count, "limit": proposal.entry_limit,
                 "primary_count": args["filled_count"]},
                result.note,
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
                 "primary_count": args["count"]}, oid or "",
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
            except (asyncio.TimeoutError, asyncio.CancelledError):
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
    ) -> None:
        self._primary = primary
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
            parts.append(
                f"{t.name}: base {t.base_contracts} ({budget}), "
                f"upsize {t.add_contracts or 'as primary'}, "
                f"cap {t.max_contracts or 'none'}"
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

    def _dispatch(self, kind: str, args: dict) -> None:
        for m in self._mirrors:
            m.start()
            m.queue.put_nowait((kind, args))

    # -- intercepted order paths -----------------------------------------
    async def execute_with_take_profit(
        self, proposal, slippage: float = 0.0, ceiling: float | None = None,
    ) -> ExecutionResult:
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
            })
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

    async def cancel_order(self, order_id: str) -> tuple[bool, str]:
        ok, note = await self._primary.cancel_order(order_id)
        self._dispatch("cancel", {"primary_order_id": order_id})
        return ok, note

    async def close(self) -> None:
        for m in self._mirrors:
            try:
                await m.close(self._drain_timeout)
            except Exception:  # noqa: BLE001
                pass
        await self._primary.close()


def targets_from_settings(settings) -> list[MirrorTarget]:
    """Read up to two mirror accounts out of the settings.

    A target is used only when BOTH its key id and its key path are present, so
    a half-filled block is ignored rather than raising at startup.
    """
    out: list[MirrorTarget] = []
    for idx in (1, 2):
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
        ))
    return out
