"""SHADOW: what the combo recovery WOULD do, recorded live, never traded.

Operator, 2026-09-27: run the combo recovery in shadow beside today's single-
leg recovery, so it is tested on days it has never seen before any money moves
on it. Nothing here places, sizes or cancels an order. It reads the live stores
READ-ONLY - the running services are neither touched nor restarted - and writes
only its own database, `runtime-combo/shadow_combo.db`.

THE RULE BEING SHADOWED, exactly as replayed in FINDINGS 98:

  * armed by a losing bot market on BTC, ETH or SOL; 5-market life per step;
  * the TRIGGER is that instrument's own trade going out anyway with its ask in
    0.70-0.79 - today's recovery conditions, unchanged;
  * the PARTNER is SOL for BTC and ETH, BTC for SOL, taken on whatever side its
    own signal showed at that instant - qualified or not - but only when that
    side was priced 0.70-0.85, and never from a reading recorded after the
    trigger's decision or more than STALE_S before it;
  * $2.00 budget, whole contracts, capped at 8, priced at the product of the
    two recorded asks (the combo orderbook's price);
  * the net-zero check: contracts x (1 - price) must clear the step's target;
  * the 3-step chain: step 1 clears the original loss L0 (a win ends it); after
    a step-1 loss, step 2 must clear at least 50% of that loss; step 3 clears L0
    again and the chain STOPS whatever happens.

WHY THE PARTNER BAND IS 0.70-0.85, and why this shadow exists at all. On the
real record a partner on its signal side at ANY price lost -7.10 against today's
recovery: the net-zero check picks cheap partners, and a cheap partner is a weak
signal. Priced 0.70-0.85 it came out +1.42. That band was read off the same
three days, so it is in-sample until this shadow says otherwise.

IT STARTS FRESH. The chain begins empty at the moment the shadow first ran
(stored in the database), so every decision it records is out of sample. The
whole history is rebuilt from the stores on each pass - deterministic, so a
restart or a missed pass changes nothing.

A DECISION IS RECORDED ONCE BOTH LEGS HAVE SETTLED. Deciding needs only what
was known at the trigger's decision instant; grading needs both outcomes, and
the chain's next step depends on the grade. So an instrument is processed up to
its first ungraded entry and resumes there on a later pass.

    pythonw scripts/shadow_combo_recovery.py            # run forever
    python  scripts/shadow_combo_recovery.py --once     # one pass
    python  scripts/shadow_combo_recovery.py --report   # results so far
    python  scripts/shadow_combo_recovery.py --report --since 2026-09-25
        # replays from an earlier start into a scratch db - a check that the
        # shadow reproduces the replay, never written to the live shadow db
"""

import argparse
import bisect
import json
import logging
import msvcrt
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import recovery_combo_replay as R  # noqa: E402  - the tested loaders

OUT = ROOT / "runtime-combo"
DB = OUT / "shadow_combo.db"
LOG = OUT / "shadow_combo.log"
LOCK = OUT / "shadow_combo.lock"

TRIGGER_BAND = (0.70, 0.79)
PARTNER_BAND = (0.70, 0.85)
PARTNERS = {"BTC": ("SOL",), "ETH": ("SOL",), "SOL": ("BTC",)}
BUDGET = 2.00
CAP = 8
WAIT_MARKETS = 5
STALE_S = 120
SECOND_SHARE = 0.50
POLL_S = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS decisions (
    instrument TEXT NOT NULL, window_open INTEGER NOT NULL,
    status TEXT NOT NULL,            -- fired | held
    reason TEXT,                     -- why held
    step INTEGER, target REAL, l0 REAL,
    trigger_ticker TEXT, trigger_side TEXT, trigger_ask REAL,
    decided_ms INTEGER,
    partner TEXT, partner_ticker TEXT, partner_side TEXT, partner_ask REAL,
    partner_decided_ms INTEGER,
    price REAL, contracts INTEGER, profit_if_right REAL,
    trigger_won INTEGER, partner_won INTEGER, combo_won INTEGER, pnl REAL,
    live_count REAL,                 -- what today's rule actually sized
    recorded_ms INTEGER,
    PRIMARY KEY (instrument, window_open)
);
"""


def day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


def contracts_for_budget(budget, price, minimum=1):
    if price <= 0:
        return minimum
    return max(minimum, int(budget / price))


def partner_book():
    """{asset: {window: (times, rows)}} from each partner's decision log."""
    book = {}
    for asset in {p for ps in PARTNERS.values() for p in ps}:
        path = ROOT / R.STORES[asset]
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        per = defaultdict(lambda: ([], []))
        for w, ms, ticker, side, ask, won in con.execute(
                "SELECT window_open, decided_ms, ticker, side, ask, won "
                "  FROM intelligence_decisions WHERE ask IS NOT NULL "
                " ORDER BY window_open, decided_ms"):
            times, rows = per[int(w)]
            times.append(int(ms))
            rows.append({"ticker": ticker, "side": side, "ask": float(ask),
                         "won": None if won is None else bool(won),
                         "decided_ms": int(ms), "asset": asset})
        con.close()
        book[asset] = dict(per)
    return book


def partner_now(book, asset, window, t):
    """(row, status): status is ok | none | pending.

    The partner's side is whatever its signal showed at t - qualified or not -
    and it counts only inside PARTNER_BAND. `pending` means a usable reading
    existed but its outcome is not graded yet: that is a reason to wait, never
    a reason to record "no partner".
    """
    slot = book.get(asset, {}).get(window)
    if not slot:
        return None, "none"
    times, rows = slot
    i = bisect.bisect_right(times, t) - 1
    if i < 0 or (t - times[i]) / 1000.0 > STALE_S:
        return None, "none"
    row = rows[i]
    assert row["decided_ms"] <= t, "partner reading from the future"
    if not PARTNER_BAND[0] <= row["ask"] <= PARTNER_BAND[1]:
        return None, "none"
    if row["won"] is None:
        return row, "pending"
    return row, "ok"


def rebuild(start_ms):
    """Every shadow decision since start_ms, rebuilt from the live stores."""
    book = partner_book()
    out = []
    for asset, partners in PARTNERS.items():
        entries = [e for e in R.load_entries(asset) if e["t"] >= start_ms]
        ep = None
        for e in entries:
            pnl = (1.0 if e["won"] else 0.0) - e["ask"]
            fired = False
            if ep is not None and ep["since"] >= WAIT_MARKETS:
                ep = None
            if ep is not None and \
                    TRIGGER_BAND[0] <= e["ask"] <= TRIGGER_BAND[1]:
                best, pending = None, False
                cands = []
                for other in partners:
                    p, status = partner_now(book, other, e["window"], e["t"])
                    if status == "pending":
                        pending = True
                    elif p is not None:
                        price = e["ask"] * p["ask"]
                        n = min(contracts_for_budget(BUDGET, price), CAP)
                        cands.append((p, price, n, n * (1.0 - price)))
                if pending and not cands:
                    break  # partner not graded yet: resume on a later pass
                ok = [c for c in cands if c[3] >= ep["target"]]
                base = {
                    "instrument": asset, "window_open": e["window"],
                    "step": ep["step"], "target": ep["target"],
                    "l0": ep["l0"], "trigger_ticker": e["ticker"],
                    "trigger_side": e["side"], "trigger_ask": e["ask"],
                    "decided_ms": e["t"], "live_count": e["count"],
                    "trigger_won": int(e["won"])}
                if not cands:
                    out.append(dict(base, status="held",
                                    reason="no partner priced 0.70-0.85 "
                                           "at that instant"))
                elif not ok:
                    out.append(dict(base, status="held",
                                    reason="failed the net-zero check"))
                else:
                    p, price, n, profit = max(ok, key=lambda c: c[0]["ask"])
                    both = bool(e["won"] and p["won"])
                    pnl = n * ((1.0 if both else 0.0) - price)
                    fired = True
                    out.append(dict(
                        base, status="fired", reason=None,
                        partner=p["asset"], partner_ticker=p["ticker"],
                        partner_side=p["side"], partner_ask=p["ask"],
                        partner_decided_ms=p["decided_ms"], price=price,
                        contracts=n, profit_if_right=profit,
                        partner_won=int(p["won"]), combo_won=int(both),
                        pnl=pnl))
                    if ep["step"] == 1:
                        ep = None if both else {
                            "step": 2, "target": SECOND_SHARE * n * price,
                            "l0": ep["l0"], "since": 0}
                    elif ep["step"] == 2:
                        ep = {"step": 3, "target": ep["l0"], "l0": ep["l0"],
                              "since": 0}
                    else:
                        ep = None
            if not fired:
                if ep is not None:
                    ep["since"] += 1
                if pnl < 0 and (ep is None or ep["step"] == 1):
                    ep = {"step": 1, "target": -pnl, "l0": -pnl, "since": 0}
    return out


def open_db(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def start_of(con, since=None):
    if since is not None:
        return since
    row = con.execute("SELECT value FROM meta WHERE key='start_ms'").fetchone()
    if row:
        return int(row[0])
    now = int(time.time() * 1000)
    con.execute("INSERT INTO meta VALUES('start_ms', ?)", (str(now),))
    con.commit()
    return now


COLUMNS = ("instrument", "window_open", "status", "reason", "step", "target",
           "l0", "trigger_ticker", "trigger_side", "trigger_ask", "decided_ms",
           "partner", "partner_ticker", "partner_side", "partner_ask",
           "partner_decided_ms", "price", "contracts", "profit_if_right",
           "trigger_won", "partner_won", "combo_won", "pnl", "live_count",
           "recorded_ms")


def write(con, rows, log=None):
    new = 0
    now = int(time.time() * 1000)
    for r in rows:
        exists = con.execute(
            "SELECT 1 FROM decisions WHERE instrument=? AND window_open=?",
            (r["instrument"], r["window_open"])).fetchone()
        vals = [r.get(c) for c in COLUMNS]
        vals[COLUMNS.index("recorded_ms")] = now
        con.execute(
            f"INSERT OR REPLACE INTO decisions ({','.join(COLUMNS)}) "
            f"VALUES ({','.join('?' * len(COLUMNS))})", vals)
        if not exists:
            new += 1
            if log:
                t = datetime.fromtimestamp(r["window_open"] / 1000,
                                           timezone.utc)
                if r["status"] == "fired":
                    log.info(
                        "SHADOW combo %s step %d  %s%s & %s%s  %d @ %.3f  "
                        "target %.2f  %s  %+.2f", f"{t:%m-%d %H:%M}",
                        r["step"], r["instrument"],
                        "+" if r["trigger_side"] == "UP" else "-",
                        r["partner"],
                        "+" if r["partner_side"] == "UP" else "-",
                        r["contracts"], r["price"], r["target"],
                        "WON" if r["combo_won"] else "LOST", r["pnl"])
                else:
                    log.info("SHADOW held %s %s step %d - %s",
                             f"{t:%m-%d %H:%M}", r["instrument"], r["step"],
                             r["reason"])
    con.commit()
    return new


def account_under_each_rule(start_ms):
    """(today's rule, combo rule) account P&L over the shadow's span."""
    original = R.partner_at

    def banded(book, asset, window, t):
        p = original(book, asset, window, t)
        if p and PARTNER_BAND[0] <= p["ask"] <= PARTNER_BAND[1]:
            return p
        return None

    saved = (R.PARTNER_MUST_QUALIFY, R.CHAIN, R.ALLOWED)
    R.PARTNER_MUST_QUALIFY, R.CHAIN, R.ALLOWED = False, True, dict(PARTNERS)
    R.partner_at = banded
    try:
        book, _ = R.load_partner_book()
        total_a = total_b = 0.0
        for asset in PARTNERS:
            entries = [e for e in R.load_entries(asset) if e["t"] >= start_ms]
            if not entries:
                continue
            ra, _ = R.simulate(asset, entries, "A", book, 1.0)
            rb, _ = R.simulate(asset, entries, "B", book, 1.0)
            total_a += sum(r["pnl"] for r in ra)
            total_b += sum(r["pnl"] for r in rb)
        return total_a, total_b
    finally:
        R.partner_at = original
        R.PARTNER_MUST_QUALIFY, R.CHAIN, R.ALLOWED = saved


def report(con, start_ms):
    rows = con.execute(
        "SELECT instrument, window_open, status, reason, step, target, "
        "       trigger_side, partner, partner_side, price, contracts, "
        "       combo_won, pnl, trigger_ask, trigger_won, live_count "
        "  FROM decisions ORDER BY window_open").fetchall()
    print("=" * 84)
    print(f"SHADOW COMBO RECOVERY - since "
          f"{datetime.fromtimestamp(start_ms / 1000, timezone.utc):%Y-%m-%d %H:%M} UTC"
          f"  (never traded)")
    print("=" * 84)
    fired = [r for r in rows if r[2] == "fired"]
    held = [r for r in rows if r[2] == "held"]
    if not rows:
        print("  nothing yet: no loss has armed a recovery with an in-band "
              "trigger since the shadow started")
        return
    for r in rows:
        t = datetime.fromtimestamp(r[1] / 1000, timezone.utc)
        if r[2] == "fired":
            print(f"  {t:%m-%d %H:%M}  {r[0]:<4} step {r[4]}  "
                  f"{r[0]}{'+' if r[6] == 'UP' else '-'} & "
                  f"{r[7]}{'+' if r[8] == 'UP' else '-'}  {r[10]} @ {r[9]:.3f}"
                  f"  target {r[5]:.2f}  {'WON ' if r[11] else 'LOST'} "
                  f"{r[12]:+.2f}")
        else:
            print(f"  {t:%m-%d %H:%M}  {r[0]:<4} step {r[4]}  held: {r[3]}")
    won = sum(1 for r in fired if r[11])
    pnl = sum(r[12] for r in fired)
    print()
    print(f"  fired {len(fired)}, won {won}, combo P&L {pnl:+.2f}   "
          f"held {len(held)}")
    # THE COMPARISON THAT MEANS SOMETHING is the whole account under each rule
    # over the same trades - not the combo against one extra contract, because
    # the two rules arm, fire and re-arm at different moments. Computed exactly
    # as FINDINGS 98 computed it, restricted to the shadow's own span.
    a, b = account_under_each_rule(start_ms)
    print(f"  same trades, whole account: today's recovery {a:+.2f}   "
          f"combo recovery {b:+.2f}   difference {b - a:+.2f}")
    days = len({day(r[1]) for r in rows})
    print(f"  {days} day(s) of shadow. FINDINGS 98 needs this to hold on days it "
          f"was never fitted to before money moves.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--since", default=None,
                    help="YYYY-MM-DD: replay from an earlier start into a "
                         "scratch db, to check the shadow reproduces the replay")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    if args.since:
        since = int(datetime.fromisoformat(args.since).replace(
            tzinfo=timezone.utc).timestamp() * 1000)
        scratch = OUT / "shadow_combo_check.db"
        if scratch.exists():
            scratch.unlink()
        con = open_db(scratch)
        write(con, rebuild(since))
        report(con, since)
        con.close()
        return

    con = open_db(DB)
    start = start_of(con)
    if args.report:
        write(con, rebuild(start))
        report(con, start)
        return

    logging.basicConfig(filename=LOG, level=logging.INFO,
                        format="%(asctime)s %(message)s")
    log = logging.getLogger("shadow")
    with LOCK.open("a+b") as lock:
        try:
            if lock.seek(0, 2) == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return  # already running
        log.info("shadow combo recovery started; chain begins %s",
                 datetime.fromtimestamp(start / 1000, timezone.utc))
        while True:
            try:
                n = write(con, rebuild(start), log)
                if n:
                    log.info("recorded %d new shadow decision(s)", n)
            except Exception as exc:  # a shadow must never die on one bad pass
                log.warning("pass failed: %s: %s", type(exc).__name__, exc)
            if args.once:
                break
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
