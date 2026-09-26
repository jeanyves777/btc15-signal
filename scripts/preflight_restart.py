"""Is it safe to restart the trading service right now?

WHY THIS EXISTS. A restart was gated on `trade_proposals.status`, and that
column does not reliably reach a terminal value - 71 rows sat at `pending` on
2026-09-21 alone, and on 2026-09-23 a `filled` row for a market that had
already closed blocked a deploy for ten minutes while the account was in fact
flat. A stale local status must never be able to hold a deploy indefinitely.

WHAT IS AUTHORITATIVE. The broker. This asks Kalshi directly for:

  * open POSITIONS - what we actually hold;
  * RESTING ORDERS - what could still fill while the service is down.

`open_mark` alone is not enough and is not used as evidence here: it is
written by the 60-second sweep, so it can be stale, and it says nothing about
working orders.

WHAT IT ALSO DOES. Ambiguous local submissions - a recovery add row that is
PENDING or PARTIAL with `placed_ms` set - are reconciled against the broker
before the verdict, because that is exactly the state whose outcome is unknown
and which must not be left unresolved across a restart.

Exit code 0 means flat and safe. 1 means something is live. 2 means the broker
could not be read, which is NOT the same as flat.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import KalshiExecutionClient  # noqa: E402
from btc15_signal.recovery_add_runner import RecoveryAddRunner  # noqa: E402
from btc15_signal import surface
from btc15_signal.store import Store  # noqa: E402


async def main() -> int:
    settings = Settings()
    store = Store(settings.database_path)
    trader = KalshiExecutionClient(
        settings.kalshi_base_url, settings.kalshi_api_key_id,
        settings.kalshi_private_key_path,
    )
    now_ms = int(__import__("time").time() * 1000)
    blockers: list[str] = []
    try:
        # 1. AMBIGUOUS SUBMISSIONS FIRST. Resolving them can only make the
        # picture clearer, and leaving one unresolved across a restart is the
        # orphan case this whole area exists to prevent.
        pending = store.adds_needing_reconciliation()
        if pending:
            print(f"reconciling {len(pending)} ambiguous add submission(s)")
            runner = RecoveryAddRunner(settings, store)
            await runner.reconcile(trader, now_ms)

        # 2. THE BROKER'S OWN VIEW.
        try:
            held, mark, per_ticker = await trader.open_mark()
        except Exception as exc:  # noqa: BLE001
            print(f"UNREADABLE: positions could not be read ({exc!r})")
            return 2
        orders, resting = await trader.resting_exposure()
        if orders < 0 or resting < 0:
            print("UNREADABLE: resting orders could not be read")
            return 2

        # ONLY WHAT THIS SYSTEM TRADES BLOCKS A RESTART OF THIS SYSTEM.
        #
        # `open_mark` reports the whole Kalshi account, and the operator trades
        # by hand in the same account. On 2026-09-25 three KXMVECROSSCATEGORY
        # positions - entirely the operator's - held preflight at NOT FLAT
        # while every 15-minute series this system trades was clear. A restart
        # cannot disturb a position in a market this process has never touched,
        # so counting those is a false block, and a false block is how a real
        # one stops being read.
        #
        # This is the same scope error as the money footer, which reported the
        # account under an instrument's name. Same fix: name the series.
        mine = {t: v for t, v in (per_ticker or {}).items()
                if surface.asset(t)}
        theirs = {t: v for t, v in (per_ticker or {}).items()
                  if not surface.asset(t)}
        held_mine = len(mine)

        print(f"broker positions : {held} total, {held_mine} in series this "
              f"system trades (marked {mark:+.4f})")
        for ticker, value in mine.items():
            print(f"    {ticker} {value:+.4f}")
        for ticker, value in theirs.items():
            print(f"    {ticker} {value:+.4f}   <- not traded by this system")
        print(f"resting orders   : {orders} (${resting:.2f} committed)")

        if held_mine:
            blockers.append(
                f"{held_mine} open position(s) in a traded series")
        if orders:
            blockers.append(f"{orders} resting order(s) at the broker")

        # 3. LOCAL STATE THAT IS STILL GENUINELY UNRESOLVED. Reported after
        # reconciliation, so anything left here is a real unknown - not a
        # stale status string.
        still = store.adds_needing_reconciliation()
        if still:
            blockers.append(
                f"{len(still)} add submission(s) still unresolved: "
                + ", ".join(r["ticker"] for r in still)
            )
        deferred = store._dicts(
            "SELECT ticker FROM recovery_adds WHERE state = "
            "'RECOVERY ADD DEFERRED'"
        )
        if deferred:
            # Not a blocker: a deferred row never sent anything, and the
            # sweeper closes it. Reported so it is not a surprise afterwards.
            print(f"deferred adds    : {len(deferred)} (nothing was sent)")
    finally:
        await trader.close()

    if blockers:
        print("\nNOT FLAT:")
        for line in blockers:
            print(f"  - {line}")
        return 1
    print("\nFLAT: no positions, no resting orders, nothing unresolved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
