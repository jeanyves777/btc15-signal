"""Record a trade made by hand in the Kalshi app, which the bot cannot see.

A manual entry is invisible to every other record in this system: the bot
placed no order, so `trade_proposals` has nothing, `executions` has nothing,
and the realised ledger does not know the money moved. Without this the
archive quietly answers "what did the rule do" when the question asked was
"what was available".

Kept deliberately separate from `trade_proposals`. A manual trade is not a
proposal the bot filled, and letting the two share a table would put rows the
strategy never chose into every measurement of the strategy - the same
boundary discipline as `mode='shadow'` on the hourly ladder.

    python scripts/record_manual_trade.py \
        --ticker KXBTC15M-26SEP211145-45 --side UP \
        --signal-ask 0.67 --fill 0.72 --best-cash-out 0.94 --settled 1.00
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.main import signal_id_for  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged  # noqa: E402


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS manual_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_ms INTEGER,
            ticker TEXT, side TEXT, signal_id TEXT,
            count REAL,
            signal_ask REAL,          -- what the bot was looking at when it declined
            fill_price REAL,          -- what was actually paid
            execution_diff REAL,      -- fill - signal, the cost of acting late
            best_cash_out REAL,       -- best exit seen before expiry
            settled_value REAL,       -- 1.0 or 0.0
            held_to_settlement INTEGER,
            fee REAL,
            net_pnl REAL,             -- settled - fill - fee, per contract
            cash_out_pnl REAL,        -- what cashing out would have returned
            hold_premium REAL,        -- net_pnl - cash_out_pnl: paid for the risk
            bot_declined_because TEXT,
            note TEXT
        )""")
    db.commit()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ticker", required=True)
    p.add_argument("--side", required=True, choices=["UP", "DOWN"])
    p.add_argument("--fill", type=float, required=True, help="price actually paid")
    p.add_argument("--settled", type=float, required=True, help="1.0 won, 0.0 lost")
    p.add_argument("--signal-ask", type=float, help="ask when the bot declined")
    p.add_argument("--best-cash-out", type=float, help="best exit seen before expiry")
    p.add_argument("--count", type=float, default=1.0)
    p.add_argument("--declined-because", default="")
    p.add_argument("--note", default="")
    p.add_argument("--db", default=None)
    args = p.parse_args()

    settings = Settings()
    db = sqlite3.connect(args.db or settings.database_path)
    ensure_schema(db)

    fee = kalshi_fee_charged(args.fill, args.count)
    net = (args.settled - args.fill) * args.count - fee
    cash = (
        (args.best_cash_out - args.fill) * args.count - fee
        if args.best_cash_out is not None
        else None
    )
    db.execute(
        "INSERT INTO manual_trades (recorded_ms,ticker,side,signal_id,count,"
        "signal_ask,fill_price,execution_diff,best_cash_out,settled_value,"
        "held_to_settlement,fee,net_pnl,cash_out_pnl,hold_premium,"
        "bot_declined_because,note) VALUES (" + ",".join("?" * 17) + ")",
        (
            int(time.time() * 1000), args.ticker, args.side,
            signal_id_for(args.ticker), args.count,
            args.signal_ask, args.fill,
            (args.fill - args.signal_ask) if args.signal_ask is not None else None,
            args.best_cash_out, args.settled,
            1, fee, net, cash,
            (net - cash) if cash is not None else None,
            args.declined_because, args.note,
        ),
    )
    db.commit()

    print(f"recorded {args.ticker} {args.side}")
    print(f"  paid            {args.fill:.4f}  (fee {fee:.4f})")
    if args.signal_ask is not None:
        print(f"  signal was at   {args.signal_ask:.4f}  "
              f"-> acting late cost {args.fill - args.signal_ask:+.4f}")
    print(f"  settled         {args.settled:.2f}")
    print(f"  NET             {net:+.4f} per contract")
    if cash is not None:
        print(f"  cashing out at  {args.best_cash_out:.2f} would have been {cash:+.4f}")
        print(f"  holding earned  {net - cash:+.4f} more, with the whole "
              f"position exposed for it")


if __name__ == "__main__":
    main()
