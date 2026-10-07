"""What the shadow instruments did, once a session, in one message.

Operator, 2026-09-28: "on telegram send just the alerts for Gold and Btc. All
the other should just come in as summary while in shadow." The shadow
instances still record every signal and every decision; they no longer send
each one (`notify.Notifier.alerts_on`). Instead the command-listening instance
(BTC) reads their stores READ-ONLY at each session close and sends one
summary: the alerts as sent, and the trades their own rules would have placed,
each graded on its settled outcome, one contract, fee-free.

The would-trade rule is the one FINDINGS 108 measured and that reproduces
BTC's real result: the first decision per window that the rule qualified, the
intelligence did not veto, and whose price had held the band 60s.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from .sessions import SESSIONS

STORES = {
    "BTC": "btc15.db", "ETH": "eth15.db", "SOL": "sol15.db", "XRP": "xrp15.db",
    "NEAR": "near15.db", "GOLD": "gold15.db", "SILVER": "silver15.db",
    "BNB": "bnb15.db",          # shadow only, since 2026-10-01
}
HOLD_S = 60


def alert_list(settings) -> set[str]:
    raw = getattr(settings, "telegram_alert_instruments", "") or ""
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def _ro(path: Path):
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5)


def auto_on(path: Path, default: bool = False) -> bool:
    """The stored `auto_trade_enabled` row - the switch the service obeys."""
    try:
        con = _ro(path)
        try:
            row = con.execute(
                "SELECT value FROM settings WHERE key='auto_trade_enabled'"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return default
    return bool(row[0]) if row else default


def mirror_flag(path: Path, name: str) -> bool:
    """Mirror `name`'s stored switch in one instrument's store; no row = on.
    Unreadable = off, so the summary never claims a copy it cannot confirm."""
    try:
        con = _ro(path)
        try:
            row = con.execute("SELECT value FROM settings WHERE key=?",
                              (f"mirror_{name}_enabled",)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(row[0]) if row else True


def mirror_copies(root: Path, settings, name: str = "m1") -> list[str]:
    """The $1 instruments mirror `name` copies now: mirroring on, the instance
    listed in MIRROR_INSTANCES, and its switch on (main.mirror_on)."""
    if not getattr(settings, "mirror_enabled", False):
        return []
    listed = {x.strip().lower()
              for x in (getattr(settings, "mirror_instances", "") or "").split(",")
              if x.strip()}
    raw = getattr(settings, "allsignal_instruments", "") or ""
    out = []
    for asset in [x.strip().upper() for x in raw.split(",") if x.strip()]:
        instance = "btc" if asset == "BTC" else asset.lower()
        if instance not in listed and not (
                asset == "BTC" and listed & {"primary", "default"}):
            continue
        path = root / STORES.get(asset, "")
        if asset in STORES and path.exists() and mirror_flag(path, name):
            out.append(asset)
    return out


def shadows(root: Path, alerting: set[str]) -> list[str]:
    """Instruments that are neither alert instruments nor auto-trading."""
    out = []
    for asset, name in STORES.items():
        path = root / name
        if asset in alerting or not path.exists():
            continue
        if not auto_on(path):
            out.append(asset)
    return out


def session_bounds(session: str, now_ms: int) -> tuple[int, int]:
    """[start, end) in ms of the most recent occurrence of `session`."""
    start_h, end_h = next((s, e) for n, s, e in SESSIONS if n == session)
    now = dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC)
    end = now.replace(minute=0, second=0, microsecond=0)
    while end.hour != end_h % 24 or end > now:
        end -= dt.timedelta(hours=1)
    start = end - dt.timedelta(hours=end_h - start_h)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _grade(n, wins, pnl):
    return {"n": n, "wins": wins, "pnl": round(pnl, 4)}


def stats(path: Path, start_ms: int, end_ms: int) -> dict:
    """Alerts and would-trade decisions in [start, end), graded. Read-only."""
    con = _ro(path)
    try:
        outcome = {}
        for w, side, won in con.execute(
                "SELECT window_open, side, won FROM observations "
                "WHERE won IS NOT NULL AND window_open >= ? AND window_open < ?",
                (start_ms, end_ms)):
            outcome.setdefault(w, {})[side] = int(won)

        def graded(w, side, won):
            if won is not None:
                return int(won)
            by = outcome.get(w) or {}
            if side in by:
                return by[side]
            return (1 - next(iter(by.values()))) if by else None

        seen, n, wins, pnl, asks, open_ = set(), 0, 0, 0.0, 0.0, 0
        for w, side, ask, won in con.execute(
                "SELECT window_open, side, our_ask, won FROM observations "
                "WHERE alerted = 1 AND our_ask IS NOT NULL AND window_open >= ? "
                "AND window_open < ? ORDER BY window_open, observed_ms",
                (start_ms, end_ms)):
            if w in seen:
                continue
            seen.add(w)
            g = graded(w, side, won)
            if g is None:
                open_ += 1
                continue
            n, wins, pnl, asks = n + 1, wins + g, pnl + g - float(ask), asks + float(ask)
        alerts = {**_grade(n, wins, pnl), "avg_ask": (asks / n) if n else 0.0,
                  "open": open_}

        first = {}
        try:
            for w, bq, fa, ask, bh, side, won in con.execute(
                    "SELECT window_open, base_qualified, final_action, ask, "
                    "band_hold_s, side, won FROM intelligence_decisions "
                    "WHERE window_open >= ? AND window_open < ? "
                    "ORDER BY window_open, decided_ms", (start_ms, end_ms)):
                if w in first or not bq or fa == "veto" or ask is None:
                    continue
                if bh is not None and bh < HOLD_S:
                    continue
                first[w] = (side, float(ask), won)
        except sqlite3.OperationalError:
            pass  # an older store without the decision table
        n2 = wins2 = 0
        pnl2 = 0.0
        for w, (side, ask, won) in first.items():
            g = graded(w, side, won)
            if g is None:
                continue
            n2, wins2, pnl2 = n2 + 1, wins2 + g, pnl2 + g - ask
        return {"alerts": alerts, "rule": _grade(n2, wins2, pnl2)}
    finally:
        con.close()


def allsignal_epoch(path: Path) -> int:
    """Fresh execution epoch, shared by all account messages, never rewrites history."""
    guard_path = path.resolve().parent / "runtime" / "daily_profit.db"
    if not guard_path.exists():
        return 0
    try:
        with _ro(guard_path) as db:
            row = db.execute("SELECT MIN(start_ms) FROM profit_days WHERE account='primary'").fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0


def allsignal_stats(path: Path, start_ms: int, end_ms: int) -> dict:
    """The all-signal strategy's session, from its own book. Read-only.

    Callers pass bounds shifted one window back (`collect_allsignal`): a
    window counts in the session in which it SETTLED, so the one closing at
    the boundary - still open when this is built - is reported, settled, in
    the next summary instead of never.
    """
    epoch_ms = allsignal_epoch(path)
    con = _ro(path)
    try:
        try:
            rows = con.execute(
                "SELECT status, won, pnl, COALESCE(fee, 0) + COALESCE(exit_fee, 0) "
                "FROM allsignal_trades "
                "WHERE window_open >= ? AND window_open < ? AND created_ms >= ?",
                (start_ms, end_ms, epoch_ms),
            ).fetchall()
        except sqlite3.OperationalError:
            return {"n": 0, "wins": 0, "pnl": 0.0, "fee": 0.0, "open": 0, "missed": 0}
    finally:
        con.close()
    graded = [r for r in rows if r[1] is not None]
    return {
        "n": len(graded), "wins": sum(int(r[1]) for r in graded),
        "pnl": round(sum(float(r[2] or 0) - float(r[3] or 0) for r in graded), 4),
        "fee": round(sum(float(r[3] or 0) for r in graded), 4),
        "open": sum(1 for r in rows if r[0] == "filled" and r[1] is None),
        "missed": sum(1 for r in rows if r[0] in ("unfilled", "failed", "claimed")),
    }


def allsignal_record(path: Path, since_ms: int | None = None) -> dict:
    """Settled all-signal trades since `since_ms` (all time if None): n, wins,
    net P&L after fees, from the fresh execution epoch. Read-only."""
    con = _ro(path)
    try:
        where, args = "WHERE won IS NOT NULL", []
        epoch_ms = allsignal_epoch(path)
        if epoch_ms:
            where += " AND created_ms >= ?"
            args.append(epoch_ms)
        if since_ms is not None:
            where += " AND window_open >= ?"
            args.append(since_ms)
        try:
            n, wins, pnl, fee = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(won),0), COALESCE(SUM(pnl),0), "
                "COALESCE(SUM(COALESCE(fee,0) + COALESCE(exit_fee,0)),0) "
                f"FROM allsignal_trades {where}", args).fetchone()
        except sqlite3.OperationalError:
            n = wins = pnl = fee = 0
    finally:
        con.close()
    return {"n": int(n or 0), "wins": int(wins or 0),
            "pnl": round(float(pnl or 0) - float(fee or 0), 4), "fee": round(float(fee or 0), 4)}


def allsignal_book(root: Path, settings, day_start_ms: int) -> list:
    """[(asset, today, overall)] for every all-signal instrument, plus the
    combined 'BTC+GOLD' row last. Read-only; each instance's own store."""
    raw = getattr(settings, "allsignal_instruments", "") or ""
    listed = [x.strip().upper() for x in raw.split(",") if x.strip()]
    rows = []
    for asset in listed:
        path = root / STORES.get(asset, "")
        if asset not in STORES or not path.exists():
            continue
        rows.append((asset, allsignal_record(path, day_start_ms), allsignal_record(path)))
    if len(rows) > 1:
        def add(a, b):
            return {k: round(a[k] + b[k], 4) if isinstance(a[k], float) else a[k] + b[k]
                    for k in a}
        today = overall = {"n": 0, "wins": 0, "pnl": 0.0, "fee": 0.0}
        for _a, t, o in rows:
            today, overall = add(today, t), add(overall, o)
        rows.append(("+".join(a for a, _t, _o in rows), today, overall))
    return rows


def collect_allsignal(root: Path, settings, session: str, now_ms: int) -> list:
    """[(asset, stats)] for every all-signal instrument with any row."""
    raw = getattr(settings, "allsignal_instruments", "") or ""
    listed = [x.strip().upper() for x in raw.split(",") if x.strip()]
    start, end = session_bounds(session, now_ms)
    start, end = start - 900_000, end - 900_000      # by settlement, see above
    out = []
    for asset in listed:
        path = root / STORES.get(asset, "")
        if asset not in STORES or not path.exists():
            continue
        try:
            s = allsignal_stats(path, start, end)
        except sqlite3.Error:
            continue
        if s["n"] or s["open"] or s["missed"]:
            out.append((asset, s))
    return out


def collect(root: Path, alerting: set[str], session: str, now_ms: int) -> list:
    """[(asset, stats)] for every shadow instrument, in STORES order."""
    start, end = session_bounds(session, now_ms)
    out = []
    for asset in shadows(root, alerting):
        try:
            out.append((asset, stats(root / STORES[asset], start, end)))
        except sqlite3.Error as exc:
            out.append((asset, {"error": f"{type(exc).__name__}"}))
    return out
