"""Export the recorded data the pair-recovery backtest needs, READ-ONLY.

Every database is opened `mode=ro`: this script cannot write to the running
system's files. It produces the three CSVs `btc15_signal.pair_recovery_backtest`
reads, and prints what it found so a gap is visible before any result is.

    python scripts/export_pair_recovery_inputs.py --out data/pair_recovery_inputs \
        --trading-db btc15.db \
        --quotes-db data/microstructure.db \
        --markets-db data/market_data.db \
        --btc-prefix KXBTC15M --gold-prefix KXGOLD15M --days 14

Sources:
- BTC trades: `fills` (broker buys: side, count, price, fee, time) joined to
  `daily_ledger` (final net P&L per market, exchange-authoritative) and
  `realised_events` (when the money became real). Markets with buys but no
  final P&L are reported and left out - never guessed.
- Quotes: `book_snapshots` (recorder) rows for either prefix. Any number of
  --quotes-db may be given, e.g. a separate GOLD recorder database.
- Outcomes: `markets` (result, settlement_ts) from any number of --markets-db,
  plus the trading DB's own `settlements` table for markets it traded.

If GOLD was recorded in a different table or shape, write quotes.csv /
outcomes.csv for it directly in the documented format and append them.
"""

import argparse
import csv
import sqlite3
import time
from pathlib import Path

WINDOW_MS = 15 * 60_000


def ro(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def market_of(ticker: str, btc: str, gold: str) -> str | None:
    if ticker.startswith(btc):
        return "BTC"
    if ticker.startswith(gold):
        return "GOLD"
    return None


def export_trades(db, since_ms: int, btc_prefix: str) -> tuple[list[dict], list[str]]:
    buys = db.execute(
        "SELECT ticker, side, count, yes_price, no_price, fee_cost, filled_ms, window_ms "
        "FROM fills WHERE action='buy' AND ticker LIKE ? AND filled_ms >= ? "
        "ORDER BY filled_ms", (btc_prefix + "%", since_ms),
    ).fetchall()
    per: dict[str, dict] = {}
    for b in buys:
        t = per.setdefault(b["ticker"], {"sides": set(), "contracts": 0.0, "cost": 0.0,
                                         "entry_ms": b["filled_ms"],
                                         "window_ms": b["window_ms"]})
        price = b["yes_price"] if b["side"] == "yes" else b["no_price"]
        t["sides"].add(b["side"])
        t["contracts"] += b["count"] or 0
        t["cost"] += (b["count"] or 0) * (price or 0) + (b["fee_cost"] or 0)
    ledger = {r["ticker"]: r for r in db.execute(
        "SELECT ticker, window_ms, pnl, source FROM daily_ledger")}
    events: dict[str, list[int]] = {}
    if has_table(db, "realised_events"):
        for r in db.execute("SELECT ticker, realised_ms FROM realised_events"):
            events.setdefault(r["ticker"], []).append(r["realised_ms"])
    rows, problems = [], []
    for ticker, t in per.items():
        led = ledger.get(ticker)
        if led is None:
            problems.append(f"{ticker}: bought but no final P&L in daily_ledger (left out)")
            continue
        if len(t["sides"]) > 1:
            problems.append(f"{ticker}: bought both sides {sorted(t['sides'])}")
        window_ms = t["window_ms"] or led["window_ms"]
        close_ms = window_ms + WINDOW_MS
        times = sorted(events.get(ticker, []))
        paid_ms = times[-1] if times else close_ms
        # Known at the last cash-out if fully exited before expiry, else at expiry.
        outcome_ms = min(close_ms, paid_ms)
        rows.append({
            "ticker": ticker, "close_ms": close_ms, "side": sorted(t["sides"])[0],
            "entry_ms": t["entry_ms"], "contracts": t["contracts"],
            "cost": round(t["cost"], 6), "net_pnl": led["pnl"],
            "outcome_ms": outcome_ms, "paid_ms": paid_ms,
            "source": f"recorded:{led['source']}",
        })
    return rows, problems


def export_quotes(dbs, since_ms, btc, gold) -> list[dict]:
    out = []
    for path in dbs:
        db = ro(path)
        if not has_table(db, "book_snapshots"):
            print(f"  {path}: no book_snapshots table")
            continue
        for r in db.execute(
            "SELECT ticker, close_ms, captured_ms, quote_ms, yes_bid, yes_ask, "
            "yes_bid_size, yes_ask_size FROM book_snapshots WHERE captured_ms >= ? "
            "ORDER BY captured_ms", (since_ms,),
        ):
            m = market_of(r["ticker"], btc, gold)
            if m is None:
                continue
            out.append({
                "market": m, "ticker": r["ticker"], "close_ms": r["close_ms"],
                # The quote's own return time, when recorded, is when it was true.
                "captured_ms": r["quote_ms"] or r["captured_ms"],
                "yes_bid": r["yes_bid"], "yes_ask": r["yes_ask"],
                "yes_bid_size": r["yes_bid_size"], "yes_ask_size": r["yes_ask_size"],
                "no_ask": "", "no_ask_size": "",
            })
    return out


def export_outcomes(dbs, trading_db, since_ms, btc, gold) -> list[dict]:
    found: dict[str, dict] = {}
    for path in dbs:
        db = ro(path)
        if not has_table(db, "markets"):
            continue
        for r in db.execute(
            "SELECT ticker, close_ms, result, settlement_ts FROM markets WHERE close_ms >= ?",
            (since_ms,),
        ):
            m = market_of(r["ticker"], btc, gold)
            if m and r["result"] in ("yes", "no"):
                found[r["ticker"]] = {"market": m, "ticker": r["ticker"],
                                      "close_ms": r["close_ms"], "result": r["result"],
                                      "settled_ms": r["settlement_ts"] or ""}
    if trading_db is not None and has_table(trading_db, "settlements"):
        for r in trading_db.execute(
            "SELECT ticker, market_result, settled_ms, window_ms FROM settlements"
        ):
            m = market_of(r["ticker"], btc, gold)
            if m and r["ticker"] not in found and r["market_result"] in ("yes", "no") \
                    and r["window_ms"]:
                found[r["ticker"]] = {"market": m, "ticker": r["ticker"],
                                      "close_ms": r["window_ms"] + WINDOW_MS,
                                      "result": r["market_result"],
                                      "settled_ms": r["settled_ms"] or ""}
    return list(found.values())


def write(path: Path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    p = argparse.ArgumentParser(description="Export pair-recovery backtest inputs (read-only)")
    p.add_argument("--out", default="data/pair_recovery_inputs")
    p.add_argument("--trading-db", default="btc15.db")
    p.add_argument("--quotes-db", action="append", default=[])
    p.add_argument("--markets-db", action="append", default=[])
    p.add_argument("--btc-prefix", default="KXBTC15M")
    p.add_argument("--gold-prefix", required=True,
                   help="ticker prefix of the GOLD 15-minute series")
    p.add_argument("--days", type=float, default=14)
    a = p.parse_args()
    since = int(time.time() * 1000 - a.days * 86_400_000)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    trading = ro(a.trading_db)
    trades, problems = export_trades(trading, since, a.btc_prefix)
    quotes = export_quotes(a.quotes_db or ["data/microstructure.db"], since,
                           a.btc_prefix, a.gold_prefix)
    outcomes = export_outcomes(a.markets_db or ["data/market_data.db"], trading, since,
                               a.btc_prefix, a.gold_prefix)

    write(out / "btc_trades.csv", trades,
          ["ticker", "close_ms", "side", "entry_ms", "contracts", "cost", "net_pnl",
           "outcome_ms", "paid_ms", "source"])
    write(out / "quotes.csv", quotes,
          ["market", "ticker", "close_ms", "captured_ms", "yes_bid", "yes_ask",
           "yes_bid_size", "yes_ask_size", "no_ask", "no_ask_size"])
    write(out / "outcomes.csv", outcomes,
          ["market", "ticker", "close_ms", "result", "settled_ms"])

    for m in ("BTC", "GOLD"):
        q = [x for x in quotes if x["market"] == m]
        o = [x for x in outcomes if x["market"] == m]
        print(f"{m}: {len(q)} quote rows over {len({x['close_ms'] for x in q})} windows; "
              f"{len(o)} settled outcomes")
    print(f"BTC recorded trades: {len(trades)}")
    for x in problems:
        print("  !", x)
    if not any(x["market"] == "GOLD" for x in quotes):
        print("  ! no GOLD quotes found - check --gold-prefix and --quotes-db")
    print(f"written to {out}")


if __name__ == "__main__":
    main()
