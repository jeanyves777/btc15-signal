"""Loss-triggered BTC/GOLD paired recovery: a fixed-rule, offline backtest.

TESTING ONLY. Nothing here places an order, reads credentials or touches the
running service. It reads three normalised CSV files (see `load_inputs`) and
writes a report, a ledger and a JSON summary.

The two arms:

  A. The existing BTC strategy alone - the RECORDED trades, replayed as they
     happened, with the broker's final net P&L.
  B. The same archive, except that a realised BTC loss switches the next
     eligible window into RECOVERY: no BTC signal trades, and instead two
     separate positions (BTC and GOLD, opposite directions) are bought 8
     minutes before the shared 15-minute expiry, until the recovery balance
     (minus the triggering loss, plus every paired net P&L) is strictly
     positive.

Arm B's normal-mode BTC trades come from the FIXED ARCHIVE. They are not
regenerated: a recorded trade that falls inside a recovery window is simply
suppressed, and once recovery exits the archive resumes as recorded. The live
system's own state (its recovery sizing, its daily limits) shaped those
recorded trades; the backtest cannot re-run that logic, and says so.

Look-ahead: an entry decision reads only quotes captured at or before the
decision time (close - 8 min). Outcomes are read only to settle a position
already taken, and recovery state changes only at the outcome's own time.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
WINDOW_MS = 15 * 60_000

# The fixed rule. Deliberately constants, not search space: this is the
# fixed-rule test, and nothing in it is tuned to the data.
BUDGET = 25.0
COMBINED_CEILING = 0.50
EVAL_BEFORE_CLOSE_MS = 8 * 60_000
# The recorder polls every 15 s; a quote older than two polls at decision time
# means the recorder missed it, not that the book was quiet.
STALE_MS = 30_000

REJECTION_ORDER = (
    "btc_quote_missing",
    "gold_quote_missing",
    "btc_quote_stale",
    "gold_quote_stale",
    "btc_ask_unavailable",
    "gold_ask_unavailable",
    "combined_ask_above_ceiling",
    "zero_contracts",
    "no_depth_at_ask",
)


# ---------------------------------------------------------------- inputs

@dataclass
class BtcTrade:
    """One RECORDED BTC market, aggregated over every buy in it."""
    ticker: str
    close_ms: int
    side: str            # "yes" (UP) / "no" (DOWN)
    entry_ms: int        # first buy fill
    contracts: float
    cost: float          # dollars paid including entry fees
    net_pnl: float       # broker's final figure - fees already inside
    outcome_ms: int      # when the result was known (exit fill or expiry)
    paid_ms: int         # when the money was available (settlement / exit)
    source: str = "recorded"


@dataclass
class Quote:
    market: str          # "BTC" / "GOLD"
    ticker: str
    close_ms: int
    captured_ms: int
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_size: float | None
    yes_ask_size: float | None
    no_ask: float | None = None
    no_ask_size: float | None = None


@dataclass
class Outcome:
    market: str
    ticker: str
    close_ms: int
    result: str          # "yes" / "no"
    settled_ms: int | None


def _f(v: str | None) -> float | None:
    if v is None or str(v).strip() in ("", "None", "null", "nan"):
        return None
    return float(v)


def _i(v: str | None) -> int | None:
    x = _f(v)
    return None if x is None else int(x)


def load_inputs(folder: Path) -> tuple[list[BtcTrade], list[Quote], list[Outcome]]:
    """Read btc_trades.csv, quotes.csv and outcomes.csv from `folder`.

    btc_trades.csv: ticker, close_ms, side, entry_ms, contracts, cost,
        net_pnl, outcome_ms, paid_ms[, source]
    quotes.csv: market, ticker, close_ms, captured_ms, yes_bid, yes_ask,
        yes_bid_size, yes_ask_size[, no_ask, no_ask_size]
    outcomes.csv: market, ticker, close_ms, result[, settled_ms]
    All times are epoch milliseconds, prices in dollars (0-1).
    """
    def rows(name: str):
        with open(folder / name, newline="", encoding="utf-8") as fh:
            yield from csv.DictReader(fh)

    trades = [
        BtcTrade(
            ticker=r["ticker"], close_ms=int(r["close_ms"]), side=r["side"].lower(),
            entry_ms=int(r["entry_ms"]), contracts=float(r["contracts"]),
            cost=float(r["cost"]), net_pnl=float(r["net_pnl"]),
            outcome_ms=int(r["outcome_ms"]), paid_ms=int(r["paid_ms"]),
            source=r.get("source") or "recorded",
        )
        for r in rows("btc_trades.csv")
    ]
    quotes = [
        Quote(
            market=r["market"].upper(), ticker=r["ticker"], close_ms=int(r["close_ms"]),
            captured_ms=int(r["captured_ms"]), yes_bid=_f(r.get("yes_bid")),
            yes_ask=_f(r.get("yes_ask")), yes_bid_size=_f(r.get("yes_bid_size")),
            yes_ask_size=_f(r.get("yes_ask_size")), no_ask=_f(r.get("no_ask")),
            no_ask_size=_f(r.get("no_ask_size")),
        )
        for r in rows("quotes.csv")
    ]
    outcomes = [
        Outcome(
            market=r["market"].upper(), ticker=r["ticker"], close_ms=int(r["close_ms"]),
            result=r["result"].lower(), settled_ms=_i(r.get("settled_ms")),
        )
        for r in rows("outcomes.csv")
    ]
    return trades, quotes, outcomes


# ---------------------------------------------------------------- the rule

def ny_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, NY).strftime("%Y-%m-%d")


def iso(ms: int | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, NY).strftime("%Y-%m-%d %H:%M:%S")


def btc_direction(close_ms: int) -> str:
    """UP on the first window of each New York day, then alternating.

    Indexed on the window's OPEN time within its NY calendar day, so the
    rotation is a property of the clock: it continues through skipped and
    normal-mode windows without any state, and DST days simply have more or
    fewer windows.
    """
    open_ms = close_ms - WINDOW_MS
    t = datetime.fromtimestamp(open_ms / 1000, NY)
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    index = int((t - midnight).total_seconds() // 900)
    return "UP" if index % 2 == 0 else "DOWN"


def selected_ask(q: Quote, direction: str) -> tuple[float | None, float | None, str]:
    """Executable ask (and size at that ask) for buying UP=YES or DOWN=NO.

    A NO ask is the complement of the YES bid on Kalshi's single book, so
    1 - yes_bid IS executable; it is used only when no direct NO ask was
    recorded, and labelled. Last-traded prices are never used.
    """
    if direction == "UP":
        return q.yes_ask, q.yes_ask_size, "yes_ask"
    if q.no_ask is not None:
        return q.no_ask, q.no_ask_size, "no_ask"
    if q.yes_bid is None:
        return None, None, "no_ask(missing)"
    return round(1.0 - q.yes_bid, 4), q.yes_bid_size, "1-yes_bid"


def _usable(price: float | None) -> bool:
    return price is not None and 0.0 < price < 1.0


def win_for(direction: str, result: str) -> bool:
    return (direction == "UP") == (result == "yes")


# ---------------------------------------------------------------- results

@dataclass
class Leg:
    market: str
    ticker: str
    direction: str
    ask: float
    ask_field: str
    quote_ms: int
    ask_size: float | None
    contracts: int
    cost: float
    result: str | None = None
    won: bool | None = None
    payout: float | None = None
    pnl: float | None = None
    settled_ms: int | None = None
    est_fee: float = 0.0


@dataclass
class Row:
    """One window of arm B, in chronological order."""
    close_ms: int
    mode: str                      # normal / recovery / recovery_pending
    cycle: int | None = None
    action: str = ""               # btc_trade / paired_entry / skip / idle / suppressed
    reasons: list[str] = field(default_factory=list)
    btc_dir: str = ""
    btc: Leg | None = None
    gold: Leg | None = None
    fill_basis: str = ""           # depth_verified / partial_depth / quote_based_unverified
    matched: int = 0
    unmatched_market: str = ""
    unmatched_contracts: int = 0
    combined_ask: float | None = None
    combined_cost: float = 0.0
    unused_cash: float = 0.0
    combined_payout: float | None = None
    combined_pnl: float | None = None
    outcome_class: str = ""
    normal_trade: BtcTrade | None = None
    normal_pnl: float | None = None
    balance_before: float | None = None
    balance_after: float | None = None
    suppressed_trade: str = ""


@dataclass
class Cycle:
    id: int
    trigger_ticker: str
    trigger_loss: float
    trigger_outcome_ms: int
    first_window_ms: int | None = None
    paired_entries: int = 0
    skipped_windows: int = 0
    windows: int = 0
    balance: float = 0.0
    low_balance: float = 0.0
    exit_outcome_ms: int | None = None
    exited: bool = False
    unresolved_entries: int = 0


def _fee(price: float, count: int) -> float:
    from .validation import kalshi_fee_charged
    return kalshi_fee_charged(price, count) if count else 0.0


def _fill(qty: int, size: float | None) -> int:
    return qty if size is None else max(0, min(qty, int(math.floor(size))))


def run_backtest(
    trades: list[BtcTrade], quotes: list[Quote], outcomes: list[Outcome],
    start_ms: int | None = None, end_ms: int | None = None,
) -> dict:
    trades = sorted(trades, key=lambda t: (t.close_ms, t.entry_ms))
    by_close_trade: dict[int, BtcTrade] = {}
    duplicates = []
    for t in trades:
        if t.close_ms in by_close_trade:
            duplicates.append(t.ticker)
        by_close_trade.setdefault(t.close_ms, t)

    q_index: dict[tuple[str, int], list[Quote]] = defaultdict(list)
    for q in quotes:
        q_index[(q.market, q.close_ms)].append(q)
    for v in q_index.values():
        v.sort(key=lambda q: q.captured_ms)
    o_index = {(o.market, o.close_ms): o for o in outcomes}

    closes = [t.close_ms for t in trades] + [q.close_ms for q in quotes] + [
        o.close_ms for o in outcomes]
    if not closes:
        raise ValueError("no input data")
    lo = start_ms if start_ms is not None else min(closes)
    hi = end_ms if end_ms is not None else max(closes)
    lo = lo - lo % WINDOW_MS + (WINDOW_MS if lo % WINDOW_MS else 0)
    grid = list(range(lo, hi + 1, WINDOW_MS))

    def latest(market: str, close_ms: int, at_ms: int) -> Quote | None:
        best = None
        for q in q_index.get((market, close_ms), []):
            if q.captured_ms <= at_ms:     # never a quote from after the decision
                best = q
            else:
                break
        return best

    rows: list[Row] = []
    cycles: list[Cycle] = []
    mode = "normal"
    cycle: Cycle | None = None
    pending_from_ms: int | None = None   # outcome time of the triggering loss
    # Positions taken but whose outcome lands later; settled in time order.
    open_pairs: list[Row] = []
    rejections_primary: Counter = Counter()
    rejections_all: Counter = Counter()

    def settle_due(now_ms: int) -> None:
        """Fold every paired outcome known by `now_ms` into its cycle."""
        nonlocal mode, cycle
        for r in sorted([r for r in open_pairs if r.close_ms <= now_ms],
                        key=lambda r: r.close_ms):
            open_pairs.remove(r)
            c = cycles[r.cycle - 1]
            if r.combined_pnl is None:
                c.unresolved_entries += 1
                continue
            r.balance_before = round(c.balance, 6)
            c.balance = round(c.balance + r.combined_pnl, 6)
            c.low_balance = min(c.low_balance, c.balance)
            r.balance_after = c.balance
            if c.balance > 1e-9 and not c.exited:
                c.exited = True
                c.exit_outcome_ms = r.close_ms
                if cycle is c:
                    cycle = None
                    mode = "normal"

    for close_ms in grid:
        eval_ms = close_ms - EVAL_BEFORE_CLOSE_MS
        open_ms = close_ms - WINDOW_MS
        # Anything whose outcome is known by the time this window opens.
        settle_due(open_ms)
        if mode == "pending" and pending_from_ms is not None and eval_ms >= pending_from_ms:
            mode = "recovery"
            pending_from_ms = None
        row = Row(close_ms=close_ms, mode=mode if mode != "pending" else "recovery_pending",
                  btc_dir=btc_direction(close_ms))
        recorded = by_close_trade.get(close_ms)

        if mode == "normal":
            row.action = "idle"
            if recorded is not None:
                row.action = "btc_trade"
                row.normal_trade = recorded
                row.normal_pnl = recorded.net_pnl
                if recorded.net_pnl < -1e-9:
                    cycle = Cycle(
                        id=len(cycles) + 1, trigger_ticker=recorded.ticker,
                        trigger_loss=round(-recorded.net_pnl, 6),
                        trigger_outcome_ms=recorded.outcome_ms,
                        balance=round(recorded.net_pnl, 6),
                        low_balance=round(recorded.net_pnl, 6),
                    )
                    cycles.append(cycle)
                    mode = "pending"
                    pending_from_ms = recorded.outcome_ms
            rows.append(row)
            continue

        # ---- recovery (or pending activation): the BTC signal does not trade.
        assert cycle is not None
        row.cycle = cycle.id
        cycle.windows += 1
        if recorded is not None:
            row.suppressed_trade = recorded.ticker
        if mode == "pending":
            row.action = "skip"
            row.reasons = ["recovery_not_yet_active"]
            cycle.skipped_windows += 1
            rows.append(row)
            continue
        if cycle.first_window_ms is None:
            cycle.first_window_ms = close_ms

        bdir = row.btc_dir
        gdir = "DOWN" if bdir == "UP" else "UP"
        bq = latest("BTC", close_ms, eval_ms)
        gq = latest("GOLD", close_ms, eval_ms)
        failed = []
        if bq is None:
            failed.append("btc_quote_missing")
        if gq is None:
            failed.append("gold_quote_missing")
        if bq is not None and eval_ms - bq.captured_ms > STALE_MS:
            failed.append("btc_quote_stale")
        if gq is not None and eval_ms - gq.captured_ms > STALE_MS:
            failed.append("gold_quote_stale")
        b_ask = b_size = g_ask = g_size = None
        b_field = g_field = ""
        if bq is not None:
            b_ask, b_size, b_field = selected_ask(bq, bdir)
            if not _usable(b_ask):
                failed.append("btc_ask_unavailable")
        if gq is not None:
            g_ask, g_size, g_field = selected_ask(gq, gdir)
            if not _usable(g_ask):
                failed.append("gold_ask_unavailable")
        if _usable(b_ask) and _usable(g_ask):
            row.combined_ask = round(b_ask + g_ask, 6)
            if row.combined_ask > COMBINED_CEILING + 1e-9:
                failed.append("combined_ask_above_ceiling")
        qty = 0
        if not failed:
            qty = int(math.floor(BUDGET / row.combined_ask + 1e-9))
            if qty <= 0:
                failed.append("zero_contracts")
        if failed:
            row.action = "skip"
            row.reasons = failed
            first = next(r for r in REJECTION_ORDER if r in failed)
            rejections_primary[first] += 1
            rejections_all.update(failed)
            cycle.skipped_windows += 1
            rows.append(row)
            continue

        # ---- entry. Depth: fill at the ask level only, never walk the book.
        b_fill, g_fill = _fill(qty, b_size), _fill(qty, g_size)
        if b_fill == 0 and g_fill == 0:
            row.action = "skip"
            row.reasons = ["no_depth_at_ask"]
            rejections_primary["no_depth_at_ask"] += 1
            rejections_all["no_depth_at_ask"] += 1
            cycle.skipped_windows += 1
            rows.append(row)
            continue
        if b_size is None or g_size is None:
            row.fill_basis = "quote_based_unverified"
        elif b_fill == qty and g_fill == qty:
            row.fill_basis = "depth_verified"
        else:
            row.fill_basis = "partial_depth"
        row.matched = min(b_fill, g_fill)
        if b_fill != g_fill:
            row.unmatched_market = "BTC" if b_fill > g_fill else "GOLD"
            row.unmatched_contracts = abs(b_fill - g_fill)

        def leg(market, q, d, ask, fld, size, n):
            return Leg(market=market, ticker=q.ticker, direction=d, ask=ask, ask_field=fld,
                       quote_ms=q.captured_ms, ask_size=size, contracts=n,
                       cost=round(n * ask, 6), est_fee=_fee(ask, n))

        row.btc = leg("BTC", bq, bdir, b_ask, b_field, b_size, b_fill)
        row.gold = leg("GOLD", gq, gdir, g_ask, g_field, g_size, g_fill)
        row.combined_cost = round(row.btc.cost + row.gold.cost, 6)
        row.unused_cash = round(BUDGET - row.combined_cost, 6)
        row.action = "paired_entry" if row.matched > 0 else "unmatched_only"
        cycle.paired_entries += 1 if row.matched > 0 else 0

        # Settlement - read only to resolve a position already taken.
        resolved = True
        for lg in (row.btc, row.gold):
            o = o_index.get((lg.market, close_ms))
            if o is None or o.result not in ("yes", "no"):
                resolved = False
                continue
            lg.result = o.result
            lg.won = win_for(lg.direction, o.result)
            lg.payout = float(lg.contracts) if lg.won else 0.0
            lg.pnl = round(lg.payout - lg.cost, 6)
            lg.settled_ms = o.settled_ms if o.settled_ms is not None else close_ms
        if resolved:
            row.combined_payout = round(row.btc.payout + row.gold.payout, 6)
            row.combined_pnl = round(row.btc.pnl + row.gold.pnl, 6)
            row.outcome_class = {
                (True, True): "both_win", (False, False): "both_loss",
                (True, False): "btc_only_win", (False, True): "gold_only_win",
            }[(row.btc.won, row.gold.won)]
        else:
            row.outcome_class = "unresolved"
        open_pairs.append(row)
        rows.append(row)

    settle_due(10**15)
    return {
        "rows": rows, "cycles": cycles, "trades": trades,
        "rejections_primary": rejections_primary, "rejections_all": rejections_all,
        "duplicate_trades": duplicates, "grid": grid, "q_index": q_index,
        "o_index": o_index,
    }


# ---------------------------------------------------------------- metrics

def _equity(events: list[tuple[int, float]]) -> tuple[float, float]:
    """(total, max drawdown) of a realised-P&L curve, peak starting at 0."""
    eq = peak = dd = 0.0
    for _, x in sorted(events, key=lambda e: e[0]):
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return round(eq, 6), round(dd, 6)


def _peak_capital(flows: list[tuple[int, int, float]]) -> float:
    """Most cash ever out at once. flows: (time, order, amount); outflows sort
    before inflows at the same instant, which is the conservative reading."""
    cash = low = 0.0
    for _, _, amt in sorted(flows, key=lambda f: (f[0], f[1])):
        cash += amt
        low = min(low, cash)
    return round(-low, 6)


def summarise(res: dict) -> dict:
    rows, cycles, trades = res["rows"], res["cycles"], res["trades"]

    # ---- A: the recorded strategy, exactly as recorded.
    a_events = [(t.outcome_ms, t.net_pnl) for t in trades]
    a_total, a_dd = _equity(a_events)
    a_flows = []
    for t in trades:
        a_flows += [(t.entry_ms, 0, -t.cost), (t.paid_ms, 1, t.cost + t.net_pnl)]
    a_daily = defaultdict(float)
    for t in trades:
        a_daily[ny_day(t.outcome_ms)] += t.net_pnl

    # ---- B: normal trades actually taken + recovery legs.
    b_normal = [r for r in rows if r.action == "btc_trade"]
    entries = [r for r in rows if r.btc is not None]
    resolved = [r for r in entries if r.combined_pnl is not None]
    b_events = [(r.normal_trade.outcome_ms, r.normal_pnl) for r in b_normal]
    b_events += [(r.close_ms, r.combined_pnl) for r in resolved]
    b_total, b_dd = _equity(b_events)
    b_flows = []
    for r in b_normal:
        t = r.normal_trade
        b_flows += [(t.entry_ms, 0, -t.cost), (t.paid_ms, 1, t.cost + t.net_pnl)]
    for r in entries:
        entry_ms = r.close_ms - EVAL_BEFORE_CLOSE_MS
        for lg in (r.btc, r.gold):
            b_flows.append((entry_ms, 0, -lg.cost))
            if lg.payout is not None:
                b_flows.append((lg.settled_ms, 1, lg.payout))
    normal_daily = defaultdict(float)
    rec_daily = defaultdict(float)
    for r in b_normal:
        normal_daily[ny_day(r.normal_trade.outcome_ms)] += r.normal_pnl
    for r in resolved:
        rec_daily[ny_day(r.close_ms)] += r.combined_pnl
    days = sorted(set(a_daily) | set(normal_daily) | set(rec_daily))
    daily = [
        {
            "day": d, "A_btc_alone": round(a_daily.get(d, 0.0), 4),
            "B_normal_btc": round(normal_daily.get(d, 0.0), 4),
            "B_recovery": round(rec_daily.get(d, 0.0), 4),
            "B_total": round(normal_daily.get(d, 0.0) + rec_daily.get(d, 0.0), 4),
        }
        for d in days
    ]

    oc = Counter(r.outcome_class for r in entries)
    recovery_rows = [r for r in rows if r.mode in ("recovery", "recovery_pending")]
    longest = None
    for c in cycles:
        end = c.exit_outcome_ms
        if end is None:
            end = rows[-1].close_ms if rows else c.trigger_outcome_ms
        dur = (end - c.trigger_outcome_ms) / 60_000
        if longest is None or dur > longest[1]:
            longest = (c.id, dur, c.windows, c.exited)

    def legsum(attr, market):
        return round(sum(getattr(getattr(r, market), attr) or 0.0 for r in resolved), 6)

    return {
        "A": {
            "trades": len(trades),
            "wins": sum(t.net_pnl > 1e-9 for t in trades),
            "losses": sum(t.net_pnl < -1e-9 for t in trades),
            "flat": sum(abs(t.net_pnl) <= 1e-9 for t in trades),
            "net_pnl": a_total, "max_drawdown": a_dd,
            "peak_capital": _peak_capital(a_flows),
        },
        "B": {
            "normal_trades": len(b_normal),
            "normal_wins": sum(r.normal_pnl > 1e-9 for r in b_normal),
            "normal_losses": sum(r.normal_pnl < -1e-9 for r in b_normal),
            "normal_net_pnl": round(sum(r.normal_pnl for r in b_normal), 6),
            "recovery_net_pnl": round(sum(r.combined_pnl for r in resolved), 6),
            "recovery_btc_leg_pnl": legsum("pnl", "btc"),
            "recovery_gold_leg_pnl": legsum("pnl", "gold"),
            "recovery_est_fees_not_deducted": round(
                sum(r.btc.est_fee + r.gold.est_fee for r in entries), 4),
            "net_pnl": b_total, "max_drawdown": b_dd,
            "peak_capital": _peak_capital(b_flows),
            "recorded_trades_suppressed": sum(1 for r in recovery_rows if r.suppressed_trade),
            "suppressed_recorded_pnl": round(sum(
                t.net_pnl for t in trades
                if t.ticker in {r.suppressed_trade for r in recovery_rows}), 6),
            "recovery_windows": len(recovery_rows),
            "paired_entries": sum(r.action == "paired_entry" for r in entries),
            "unmatched_only_entries": sum(r.action == "unmatched_only" for r in entries),
            "skipped_windows": sum(r.action == "skip" for r in recovery_rows),
            "outcomes": {k: oc.get(k, 0) for k in
                         ("both_win", "both_loss", "btc_only_win", "gold_only_win",
                          "unresolved")},
            "fill_basis": dict(Counter(r.fill_basis for r in entries)),
            "unmatched_contracts": sum(r.unmatched_contracts for r in entries),
            "unmatched_pnl": round(sum(
                (getattr(r, r.unmatched_market.lower()).pnl or 0.0)
                * r.unmatched_contracts / max(1, getattr(r, r.unmatched_market.lower()).contracts)
                for r in resolved if r.unmatched_contracts), 6),
            "rejections_primary": dict(res["rejections_primary"]),
            "rejections_all_conditions": dict(res["rejections_all"]),
            "pending_activation_skips": sum(
                "recovery_not_yet_active" in r.reasons for r in recovery_rows),
        },
        "cycles": {
            "completed": sum(c.exited for c in cycles),
            "unfinished": sum(not c.exited for c in cycles),
            "longest": None if longest is None else {
                "cycle": longest[0], "minutes": round(longest[1], 1),
                "windows": longest[2], "completed": longest[3]},
        },
        "daily": daily,
    }


# ---------------------------------------------------------------- coverage

def coverage(res: dict) -> dict:
    grid, q, o = res["grid"], res["q_index"], res["o_index"]
    out = {"windows_in_period": len(grid)}
    for m in ("BTC", "GOLD"):
        out[f"{m}_windows_with_quotes"] = sum((m, c) in q for c in grid)
        out[f"{m}_windows_with_outcome"] = sum((m, c) in o for c in grid)
        sizes = [x for c in grid for x in q.get((m, c), [])]
        out[f"{m}_quote_rows"] = len(sizes)
        out[f"{m}_quote_rows_with_depth"] = sum(
            x.yes_ask_size is not None and x.yes_bid_size is not None for x in sizes)
    missing = [c for c in grid if ("BTC", c) not in q or ("GOLD", c) not in q]
    out["windows_missing_a_quote_series"] = len(missing)
    out["first_window_close"] = iso(grid[0]) if grid else ""
    out["last_window_close"] = iso(grid[-1]) if grid else ""
    return out


# ---------------------------------------------------------------- validation

def invariants(res: dict, summary: dict) -> list[str]:
    """Re-derive every money figure from its parts. Returns failures."""
    bad = []
    for r in res["rows"]:
        if r.btc is None:
            continue
        eval_ms = r.close_ms - EVAL_BEFORE_CLOSE_MS
        for lg in (r.btc, r.gold):
            if lg.quote_ms > eval_ms:
                bad.append(f"{iso(r.close_ms)} {lg.market}: quote after decision")
            if abs(lg.cost - lg.contracts * lg.ask) > 1e-6:
                bad.append(f"{iso(r.close_ms)} {lg.market}: cost != contracts*ask")
            if lg.payout is not None:
                if abs(lg.payout - (lg.contracts if lg.won else 0)) > 1e-9:
                    bad.append(f"{iso(r.close_ms)} {lg.market}: payout")
                if abs(lg.pnl - (lg.payout - lg.cost)) > 1e-6:
                    bad.append(f"{iso(r.close_ms)} {lg.market}: pnl != payout-cost")
        if r.combined_cost > BUDGET + 1e-6:
            bad.append(f"{iso(r.close_ms)}: over budget")
        if r.combined_ask is not None and r.combined_ask > COMBINED_CEILING + 1e-9:
            bad.append(f"{iso(r.close_ms)}: entered above ceiling")
        if r.combined_pnl is not None and abs(
                r.combined_pnl - (r.btc.pnl + r.gold.pnl)) > 1e-6:
            bad.append(f"{iso(r.close_ms)}: combined pnl")
        if r.btc_dir != r.btc.direction or r.gold.direction == r.btc.direction:
            bad.append(f"{iso(r.close_ms)}: direction rule")
    # Cycle balances re-folded from the ledger alone.
    for c in res["cycles"]:
        bal = -c.trigger_loss
        for r in res["rows"]:
            if r.cycle == c.id and r.combined_pnl is not None:
                bal += r.combined_pnl
        if abs(bal - c.balance) > 1e-6:
            bad.append(f"cycle {c.id}: balance {c.balance} != refold {bal}")
    a, b = summary["A"], summary["B"]
    if abs(a["net_pnl"] - sum(t.net_pnl for t in res["trades"])) > 1e-6:
        bad.append("A total != sum of recorded net P&L")
    if abs(b["net_pnl"] - (b["normal_net_pnl"] + b["recovery_net_pnl"])) > 1e-6:
        bad.append("B total != normal + recovery")
    if abs(b["recovery_net_pnl"] - (b["recovery_btc_leg_pnl"]
                                    + b["recovery_gold_leg_pnl"])) > 1e-6:
        bad.append("recovery total != BTC legs + GOLD legs")
    return bad


def examples(res: dict, per_class: int = 2) -> list[Row]:
    seen = Counter()
    picked = []
    for cls in ("btc_only_win", "gold_only_win", "both_win", "both_loss"):
        for r in res["rows"]:
            if r.outcome_class == cls and seen[cls] < per_class:
                picked.append(r)
                seen[cls] += 1
    return picked


# ---------------------------------------------------------------- output

LEDGER_COLUMNS = [
    "window_close_ny", "mode", "cycle", "action", "reasons", "suppressed_recorded_trade",
    "normal_ticker", "normal_side", "normal_contracts", "normal_cost", "normal_net_pnl",
    "btc_ticker", "btc_dir", "btc_ask", "btc_ask_field", "btc_quote_ny", "btc_ask_size",
    "btc_contracts", "btc_cost", "btc_result", "btc_payout", "btc_pnl",
    "gold_ticker", "gold_dir", "gold_ask", "gold_ask_field", "gold_quote_ny", "gold_ask_size",
    "gold_contracts", "gold_cost", "gold_result", "gold_payout", "gold_pnl",
    "combined_ask", "matched_contracts", "unmatched_market", "unmatched_contracts",
    "fill_basis", "combined_cost", "unused_cash", "combined_payout", "combined_pnl",
    "outcome_class", "est_fees_not_deducted", "balance_before", "balance_after",
]


def ledger_rows(res: dict) -> list[dict]:
    out = []
    for r in res["rows"]:
        if r.action == "idle":
            continue
        d = {k: "" for k in LEDGER_COLUMNS}
        d.update(window_close_ny=iso(r.close_ms), mode=r.mode, cycle=r.cycle or "",
                 action=r.action, reasons=";".join(r.reasons),
                 suppressed_recorded_trade=r.suppressed_trade,
                 btc_dir=r.btc_dir if r.mode != "normal" else "")
        if r.normal_trade:
            t = r.normal_trade
            d.update(normal_ticker=t.ticker, normal_side=t.side,
                     normal_contracts=t.contracts, normal_cost=round(t.cost, 4),
                     normal_net_pnl=round(t.net_pnl, 4))
        for name, lg in (("btc", r.btc), ("gold", r.gold)):
            if lg is None:
                continue
            d.update({
                f"{name}_ticker": lg.ticker, f"{name}_dir": lg.direction,
                f"{name}_ask": lg.ask, f"{name}_ask_field": lg.ask_field,
                f"{name}_quote_ny": iso(lg.quote_ms), f"{name}_ask_size": lg.ask_size,
                f"{name}_contracts": lg.contracts, f"{name}_cost": round(lg.cost, 4),
                f"{name}_result": lg.result or "", f"{name}_payout": lg.payout,
                f"{name}_pnl": None if lg.pnl is None else round(lg.pnl, 4),
            })
        if r.btc is not None:
            d.update(combined_ask=r.combined_ask, matched_contracts=r.matched,
                     unmatched_market=r.unmatched_market,
                     unmatched_contracts=r.unmatched_contracts, fill_basis=r.fill_basis,
                     combined_cost=round(r.combined_cost, 4),
                     unused_cash=round(r.unused_cash, 4),
                     combined_payout=r.combined_payout,
                     combined_pnl=None if r.combined_pnl is None else round(r.combined_pnl, 4),
                     outcome_class=r.outcome_class,
                     est_fees_not_deducted=round(r.btc.est_fee + r.gold.est_fee, 4),
                     balance_before=r.balance_before, balance_after=r.balance_after)
        elif r.combined_ask is not None:
            d.update(combined_ask=r.combined_ask)
        out.append(d)
    return out


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2f}"


def render_report(res: dict, s: dict, cov: dict, bad: list[str], label: str) -> str:
    A, B, C = s["A"], s["B"], s["cycles"]
    L = []
    w = L.append
    w("# BTC loss-triggered BTC/GOLD recovery: fixed-rule backtest\n")
    w(f"Data: {label}. TESTING ONLY - no strategy change, no orders.\n")
    w("## Fixed rule under test\n")
    w(f"- Budget ${BUDGET:.2f} per paired entry, shared; qty = floor(25 / (BTC ask + GOLD ask)), "
      "same on both legs; unused budget kept as cash.")
    w(f"- Decision exactly {EVAL_BEFORE_CLOSE_MS // 60000} min before the shared 15-min expiry, "
      f"latest quote captured at or before that instant, stale if older than {STALE_MS // 1000}s.")
    w(f"- Combined selected-side executable ask <= ${COMBINED_CEILING:.2f}.")
    w("- BTC UP on the first window of each New York day, alternating per window; GOLD opposite.")
    w("- Held to settlement. Leg P&L = payout - cost; Kalshi fees are NOT deducted from recovery "
      "legs (rule as specified); the estimate is shown separately.\n")
    w("## Headline (full period)\n")
    w("| | A. BTC alone (recorded) | B. BTC + recovery |")
    w("|---|---:|---:|")
    w(f"| Net P&L | {_money(A['net_pnl'])} | {_money(B['net_pnl'])} |")
    w(f"| - of which normal BTC | {_money(A['net_pnl'])} | {_money(B['normal_net_pnl'])} |")
    w(f"| - of which recovery | - | {_money(B['recovery_net_pnl'])} "
      f"(BTC legs {_money(B['recovery_btc_leg_pnl'])}, GOLD legs "
      f"{_money(B['recovery_gold_leg_pnl'])}) |")
    w(f"| BTC wins / losses | {A['wins']} / {A['losses']} | "
      f"{B['normal_wins']} / {B['normal_losses']} |")
    w(f"| Max drawdown | {A['max_drawdown']:.2f} | {B['max_drawdown']:.2f} |")
    w(f"| Peak capital required (incl. unsettled) | {A['peak_capital']:.2f} | "
      f"{B['peak_capital']:.2f} |\n")
    w("Peak capital = the most cash ever out at once: realised losses to date plus the "
      "stake in every position not yet paid (payment time, not outcome time).\n")
    w(f"Recovery fees not deducted (estimate at Kalshi taker schedule): "
      f"${B['recovery_est_fees_not_deducted']:.2f}. B net after that estimate: "
      f"{_money(B['net_pnl'] - B['recovery_est_fees_not_deducted'])}.\n")
    w(f"Recorded BTC trades suppressed by recovery in B: {B['recorded_trades_suppressed']} "
      f"(their recorded net P&L: {_money(B['suppressed_recorded_pnl'])}).\n")
    w("## Recovery activity\n")
    o = B["outcomes"]
    w(f"- Recovery windows: {B['recovery_windows']}; paired entries: {B['paired_entries']}; "
      f"unmatched-only entries: {B['unmatched_only_entries']}; skipped: {B['skipped_windows']} "
      f"(of which awaiting activation: {B['pending_activation_skips']}).")
    w(f"- Both win {o['both_win']}, both lose {o['both_loss']}, BTC-only win {o['btc_only_win']}, "
      f"GOLD-only win {o['gold_only_win']}, unresolved {o['unresolved']}.")
    lg = C["longest"]
    longest = "none" if lg is None else (
        f"cycle {lg['cycle']}, {lg['minutes'] / 60:.1f} h over {lg['windows']} windows"
        f"{'' if lg['completed'] else ' (still open)'}")
    w(f"- Cycles completed {C['completed']}, unfinished {C['unfinished']}. Longest recovery: "
      f"{longest}.")
    w(f"- Fill basis: {B['fill_basis']}. Unmatched-leg contracts: {B['unmatched_contracts']} "
      f"(their P&L inside recovery: {_money(B['unmatched_pnl'])}).\n")
    w("### Skipped windows by condition\n")
    w("| Condition | First failing | Any failing |")
    w("|---|---:|---:|")
    for k in REJECTION_ORDER:
        w(f"| {k} | {B['rejections_primary'].get(k, 0)} | "
          f"{B['rejections_all_conditions'].get(k, 0)} |")
    w("")
    w("## Cycles\n")
    w("| # | Trigger | Loss | Recovery start | Paired | Skipped | Windows | Duration (min) "
      "| Result |")
    w("|---|---|---:|---|---:|---:|---:|---:|---|")
    last = res["rows"][-1].close_ms if res["rows"] else 0
    for c in res["cycles"]:
        end = c.exit_outcome_ms or last
        result = (f"surplus {c.balance:+.2f}" if c.exited
                  else f"UNFINISHED, debt {c.balance:+.2f}")
        w(f"| {c.id} | {c.trigger_ticker} | {c.trigger_loss:.2f} | {iso(c.first_window_ms)} | "
          f"{c.paired_entries} | {c.skipped_windows} | {c.windows} | "
          f"{(end - c.trigger_outcome_ms) / 60000:.0f} | {result} |")
    w("")
    w("## Daily net P&L (New York day of outcome)\n")
    w("| Day | A. BTC alone | B. normal BTC | B. recovery | B. total |")
    w("|---|---:|---:|---:|---:|")
    for d in s["daily"]:
        w(f"| {d['day']} | {d['A_btc_alone']:+.2f} | {d['B_normal_btc']:+.2f} | "
          f"{d['B_recovery']:+.2f} | {d['B_total']:+.2f} |")
    w(f"| **Total** | {A['net_pnl']:+.2f} | {B['normal_net_pnl']:+.2f} | "
      f"{B['recovery_net_pnl']:+.2f} | {B['net_pnl']:+.2f} |\n")
    w("## Reconciliation examples (mixed results first)\n")
    for r in examples(res):
        b, g = r.btc, r.gold
        w(f"**{iso(r.close_ms)} - {r.outcome_class}** (cycle {r.cycle}, {r.fill_basis})")
        for lg in (b, g):
            w(f"- {lg.market} {lg.direction} {lg.ticker}: quote {iso(lg.quote_ms)} "
              f"{lg.ask_field}={lg.ask:.2f}; {lg.contracts} x {lg.ask:.2f} = cost "
              f"{lg.cost:.2f}; result {lg.result} -> {'WIN' if lg.won else 'LOSS'}; payout "
              f"{lg.payout:.2f}; P&L {lg.payout:.2f} - {lg.cost:.2f} = {lg.pnl:+.2f}")
        w(f"- Combined: ask {r.combined_ask:.2f}, cost {r.combined_cost:.2f}, unused cash "
          f"{r.unused_cash:.2f}, payout {r.combined_payout:.2f}, P&L {b.pnl:+.2f} + {g.pnl:+.2f} "
          f"= {r.combined_pnl:+.2f}; recovery balance {r.balance_before:+.2f} -> "
          f"{r.balance_after:+.2f}\n")
    w("## Data coverage\n")
    for k, v in cov.items():
        w(f"- {k}: {v}")
    if res["duplicate_trades"]:
        w(f"- Recorded BTC trades sharing a window (only the first replayed): "
          f"{res['duplicate_trades']}")
    w("")
    w("## Validation\n")
    w("- Entry decisions read only quotes captured at or before close - 8 min "
      "(checked per row below).")
    w("- Outcomes resolve only positions already taken; recovery state changes at the "
      "outcome time, and the next window is the first that can see it.")
    w("- A is the actual recorded BTC replay. B is a counterfactual: its normal-mode BTC trades "
      "are replayed from the FIXED ARCHIVE (not regenerated); recorded trades inside recovery "
      "windows are suppressed. The live system's own state (its recovery sizing, daily limits) "
      "shaped the archive and is not re-simulated.")
    w("- Recovery fills: depth-verified when the ask-level size was recorded for both legs, "
      "otherwise quote-based with unverified fills. Never walks the book.")
    w(f"- Automated reconciliation: {'ALL CHECKS PASSED' if not bad else 'FAILURES:'}")
    for x in bad:
        w(f"  - {x}")
    w("- No parameter was optimised: every threshold above is the fixed rule.")
    return "\n".join(L) + "\n"


def write_outputs(res: dict, out: Path, label: str) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    s = summarise(res)
    cov = coverage(res)
    bad = invariants(res, s)
    (out / "report.md").write_text(render_report(res, s, cov, bad, label), encoding="utf-8")
    with open(out / "ledger.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
        wr.writeheader()
        wr.writerows(ledger_rows(res))
    cyc = [asdict(c) for c in res["cycles"]]
    (out / "summary.json").write_text(json.dumps(
        {"summary": s, "coverage": cov, "cycles": cyc, "invariant_failures": bad},
        indent=2, default=str), encoding="utf-8")
    return {"summary": s, "coverage": cov, "invariant_failures": bad}


def run() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("inputs", help="folder holding btc_trades.csv, quotes.csv, outcomes.csv")
    p.add_argument("--out", default="reports/pair_recovery")
    p.add_argument("--label", default="")
    a = p.parse_args()
    trades, quotes, outcomes = load_inputs(Path(a.inputs))
    res = run_backtest(trades, quotes, outcomes)
    r = write_outputs(res, Path(a.out), a.label or a.inputs)
    s = r["summary"]
    print(f"A net {s['A']['net_pnl']:+.2f}  B net {s['B']['net_pnl']:+.2f}  "
          f"cycles {s['cycles']}  invariant failures {len(r['invariant_failures'])}")
    print(f"report: {Path(a.out) / 'report.md'}")


if __name__ == "__main__":
    run()
