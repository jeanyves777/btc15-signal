"""Export the recorded data the pair-recovery backtest needs, READ-ONLY.

Every database is opened `mode=ro`: this script cannot write to the running
system's files. It produces the three CSVs `btc15_signal.pair_recovery_backtest`
reads, and prints what it found so a gap is visible before any result is.

GOLD runs as its own instance (`scripts/run_gold.ps1`): series KXGOLD15M,
database `gold15.db`, market cache `data/market_data_kxgold15m.db`. BTC is the
default instance: `btc15.db`, cache `data/market_data.db`. So the two
instruments are exported from two separate trading databases.

    python scripts/export_pair_recovery_inputs.py --out data/pair_recovery_inputs \
        --btc-db btc15.db --gold-db gold15.db

Sources (all read-only):
- BTC trades (arm A): `fills` (broker buys: side, count, price, fee, time)
  joined to `daily_ledger` (final net P&L per market, exchange-authoritative)
  and `realised_events` (when the money became real). Markets bought but with
  no final P&L are reported and left out - never guessed.
- Quotes: each instrument's `observations` table - the per-poll executable
  `yes_ask`/`no_ask` captured inside the entry window, with `observed_ms` as
  the capture time. These are quotes, not depth, so the backtest marks their
  fills quote-based/unverified. If a recorder `book_snapshots` database is
  given with --quotes-db, its depth rows are added too and preferred.
- Outcomes: each instrument's `settlements` (market_result, settled_ms,
  window_ms); the market cache (`markets` table) is added when given with
  --markets-db.

Columns written match `pair_recovery_backtest.load_inputs`.
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


def export_trades(db, since_ms: int, prefix: str) -> tuple[list[dict], list[str]]:
    """BTC arm A: every recorded BTC market, aggregated over its buys."""
    buys = db.execute(
        "SELECT ticker, side, count, yes_price, no_price, fee_cost, filled_ms, window_ms "
        "FROM fills WHERE action='buy' AND ticker LIKE ? AND filled_ms >= ? "
        "ORDER BY filled_ms", (prefix + "%", since_ms),
    ).fetchall()
    per: dict[str, dict] = {}
    for b in buys:
        t = per.setdefault(b["ticker"], {"sides": set(), "contracts": 0.0, "cost": 0.0,
                                         "entry_ms": b["filled_ms"], "window_ms": b["window_ms"]})
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
        outcome_ms = min(close_ms, paid_ms)
        rows.append({
            "ticker": ticker, "close_ms": close_ms, "side": sorted(t["sides"])[0],
            "entry_ms": t["entry_ms"], "contracts": t["contracts"],
            "cost": round(t["cost"], 6), "net_pnl": led["pnl"],
            "outcome_ms": outcome_ms, "paid_ms": paid_ms, "source": f"recorded:{led['source']}",
        })
    return rows, problems


def quotes_from_observations(db, market: str, since_ms: int) -> list[dict]:
    """Executable asks per poll, from the trading DB's observation archive."""
    if not has_table(db, "observations"):
        return []
    out = []
    for r in db.execute(
        "SELECT window_open, observed_ms, ticker, yes_ask, no_ask FROM observations "
        "WHERE observed_ms >= ? AND ticker IS NOT NULL ORDER BY observed_ms", (since_ms,),
    ):
        out.append({
            "market": market, "ticker": r["ticker"], "close_ms": r["window_open"] + WINDOW_MS,
            "captured_ms": r["observed_ms"],
            # observations store the two side asks directly; no bid, no depth.
            "yes_bid": "", "yes_ask": r["yes_ask"],
            "yes_bid_size": "", "yes_ask_size": "",
            "no_ask": r["no_ask"], "no_ask_size": "",
        })
    return out


def quotes_from_book(path: str, market: str, prefix: str, since_ms: int) -> list[dict]:
    db = ro(path)
    if not has_table(db, "book_snapshots"):
        print(f"  {path}: no book_snapshots table")
        return []
    out = []
    for r in db.execute(
        "SELECT ticker, close_ms, captured_ms, quote_ms, yes_bid, yes_ask, "
        "yes_bid_size, yes_ask_size FROM book_snapshots WHERE captured_ms >= ? "
        "AND ticker LIKE ? ORDER BY captured_ms", (since_ms, prefix + "%"),
    ):
        out.append({
            "market": market, "ticker": r["ticker"], "close_ms": r["close_ms"],
            "captured_ms": r["quote_ms"] or r["captured_ms"],
            "yes_bid": r["yes_bid"], "yes_ask": r["yes_ask"],
            "yes_bid_size": r["yes_bid_size"], "yes_ask_size": r["yes_ask_size"],
            "no_ask": "", "no_ask_size": "",
        })
    return out


def outcomes_from_trading(db, market: str, since_ms: int) -> dict[str, dict]:
    found: dict[str, dict] = {}
    if has_table(db, "settlements"):
        for r in db.execute(
            "SELECT ticker, market_result, settled_ms, window_ms FROM settlements "
            "WHERE window_ms >= ?", (since_ms - WINDOW_MS,),
        ):
            if r["market_result"] in ("yes", "no") and r["window_ms"]:
                found[r["ticker"]] = {
                    "market": market, "ticker": r["ticker"],
                    "close_ms": r["window_ms"] + WINDOW_MS, "result": r["market_result"],
                    "settled_ms": r["settled_ms"] or ""}
    return found


def outcomes_from_cache(path: str, market: str, prefix: str, since_ms: int) -> dict[str, dict]:
    db = ro(path)
    if not has_table(db, "markets"):
        return {}
    found: dict[str, dict] = {}
    for r in db.execute(
        "SELECT ticker, close_ms, result, settlement_ts FROM markets "
        "WHERE close_ms >= ? AND ticker LIKE ?", (since_ms, prefix + "%"),
    ):
        if r["result"] in ("yes", "no"):
            found[r["ticker"]] = {"market": market, "ticker": r["ticker"],
                                  "close_ms": r["close_ms"], "result": r["result"],
                                  "settled_ms": r["settlement_ts"] or ""}
    return found


def write(path: Path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    p = argparse.ArgumentParser(description="Export pair-recovery backtest inputs (read-only)")
    p.add_argument("--out", default="data/pair_recovery_inputs")
    p.add_argument("--btc-db", default="btc15.db",
                   help="BTC trading DB (fills, observations, settlements)")
    p.add_argument("--gold-db", default="gold15.db",
                   help="GOLD trading DB (observations, settlements)")
    p.add_argument("--btc-prefix", default="KXBTC15M")
    p.add_argument("--gold-prefix", default="KXGOLD15M")
    p.add_argument("--btc-cache", default="data/market_data.db")
    p.add_argument("--gold-cache", default="data/market_data_kxgold15m.db")
    p.add_argument("--quotes-db", action="append", default=[],
                   help="optional recorder book_snapshots DB(s) adding depth")
    p.add_argument("--days", type=float, default=15)
    a = p.parse_args()
    since = int(time.time() * 1000 - a.days * 86_400_000)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    btc = ro(a.btc_db)
    gold = ro(a.gold_db)

    trades, problems = export_trades(btc, since, a.btc_prefix)

    quotes = quotes_from_observations(btc, "BTC", since)
    quotes += quotes_from_observations(gold, "GOLD", since)
    for path in a.quotes_db:
        quotes += quotes_from_book(path, "BTC", a.btc_prefix, since)
        quotes += quotes_from_book(path, "GOLD", a.gold_prefix, since)

    outcomes = outcomes_from_trading(btc, "BTC", since)
    outcomes.update(outcomes_from_trading(gold, "GOLD", since))
    for market, path, prefix in (("BTC", a.btc_cache, a.btc_prefix),
                                 ("GOLD", a.gold_cache, a.gold_prefix)):
        if Path(path).exists():
            for tk, row in outcomes_from_cache(path, market, prefix, since).items():
                outcomes.setdefault(tk, row)

    write(out / "btc_trades.csv", trades,
          ["ticker", "close_ms", "side", "entry_ms", "contracts", "cost", "net_pnl",
           "outcome_ms", "paid_ms", "source"])
    write(out / "quotes.csv", quotes,
          ["market", "ticker", "close_ms", "captured_ms", "yes_bid", "yes_ask",
           "yes_bid_size", "yes_ask_size", "no_ask", "no_ask_size"])
    write(out / "outcomes.csv", list(outcomes.values()),
          ["market", "ticker", "close_ms", "result", "settled_ms"])

    for m in ("BTC", "GOLD"):
        q = [x for x in quotes if x["market"] == m]
        o = [x for x in outcomes.values() if x["market"] == m]
        depth = sum(x["yes_ask_size"] not in ("", None) for x in q)
        print(f"{m}: {len(q)} quote rows over {len({x['close_ms'] for x in q})} windows "
              f"({depth} with depth); {len(o)} settled outcomes")
    print(f"BTC recorded trades: {len(trades)}")
    for x in problems:
        print("  !", x)
    if not any(x["market"] == "GOLD" for x in quotes):
        print("  ! no GOLD quotes - check --gold-db path and that gold recorded observations")
    print(f"written to {out}")


if __name__ == "__main__":
    main()
