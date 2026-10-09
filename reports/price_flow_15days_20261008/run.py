"""Chronological observed-quote replay of universal 85c price confirmation.

Production databases are opened read-only. Observed asks prove a displayed quote,
not a fill or available size, so results are counterfactual rather than broker P&L.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged

NY = ZoneInfo("America/New_York")
START = int(datetime(2026, 9, 24, tzinfo=NY).timestamp() * 1000)
END = int(datetime(2026, 10, 8, 21, 15, tzinfo=NY).timestamp() * 1000)
CAPITAL = 708.75
TARGET = CAPITAL * .03
FLOOR = .85
CUTOFF_S = 120


def ro(path: Path):
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("pragma query_only=on")
    db.execute("begin")
    return db


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, NY).isoformat()


def load():
    db = ro(ROOT / "btc15.db")
    signals = [dict(r) for r in db.execute("""
        SELECT a.window_open wo,a.created_at at,o.side,o.our_ask ask,
               o.yes_ask,o.no_ask,o.ticker,p.won
        FROM strategy_alerts a
        JOIN observations o ON o.window_open=a.window_open AND o.observed_ms=a.created_at
        JOIN predictions p ON p.window_open=a.window_open
        WHERE a.strategy='primary' AND a.window_open>=? AND a.window_open<?
          AND a.window_open+900000<=? AND p.won IS NOT NULL
          AND p.side=o.side AND o.our_ask>0 AND o.our_ask<1
        ORDER BY a.window_open
    """, (START, END, END))]
    assert len(signals) == len({r["wo"] for r in signals})
    polls = defaultdict(list)
    for r in db.execute("""
        SELECT window_open wo,observed_ms at,yes_ask,no_ask,remaining_s
        FROM observations WHERE window_open>=? AND window_open<?
        ORDER BY observed_ms
    """, (START, END)):
        polls[r["wo"]].append(dict(r))
    db.close()
    return signals, polls


def known_result(trade, now):
    return trade["won"] if trade["known"] <= now else None


def boosted(trades, now):
    since = None
    for trade in trades:
        result = known_result(trade, now)
        since = 0 if result is False else (None if since is None else since + 1)
    return since is not None and since < 2


def flow_entry(signal, rows):
    """First observed side at >=85c; original side wins an exact timestamp tie."""
    side = signal["side"]
    candidates = []
    initial = signal["yes_ask"] if side == "UP" else signal["no_ask"]
    opposite = signal["no_ask"] if side == "UP" else signal["yes_ask"]
    if initial and FLOOR <= initial < 1:
        return signal["at"], side, initial, False
    if opposite and FLOOR <= opposite < 1:
        return signal["at"], "DOWN" if side == "UP" else "UP", opposite, True
    for row in rows:
        if row["at"] < signal["at"]:
            continue
        if row["at"] >= signal["wo"] + 900_000 or row["remaining_s"] < CUTOFF_S:
            break
        ordered = ((side, row["yes_ask"] if side == "UP" else row["no_ask"]),
                   ("DOWN" if side == "UP" else "UP",
                    row["no_ask"] if side == "UP" else row["yes_ask"]))
        for chosen, ask in ordered:
            if ask and FLOOR <= ask < 1:
                return row["at"], chosen, ask, chosen != side
    return None


def replay(signals, polls, mode, slippage=0.0, target=True):
    by_day = defaultdict(list)
    for s in signals:
        by_day[stamp(s["wo"])[:10]].append(s)
    ledger, days = [], []
    for day, rows in sorted(by_day.items()):
        trades = []
        pnl = peak = drawdown = 0.0
        target_hit = None
        missed = Counter()

        def flush(now):
            nonlocal pnl, peak, drawdown, target_hit
            for t in sorted(trades, key=lambda x: x["known"]):
                if not t["booked"] and t["known"] <= now:
                    t["booked"] = True
                    pnl += t["net"]
                    peak = max(peak, pnl)
                    drawdown = max(drawdown, peak - pnl)
                    if target and target_hit is None and pnl + 1e-9 >= TARGET:
                        target_hit = t["known"]

        for s in rows:
            flush(s["at"])
            if target_hit:
                missed["daily_target"] += 1
                continue
            after_known_loss = boosted(trades, s["at"])
            if mode == "all_trades" or (mode == "wait_85_after_loss" and not after_known_loss):
                entry = (s["at"], s["side"], s["ask"], False)
            else:
                entry = flow_entry(s, polls[s["wo"]])
                if not entry:
                    missed["neither_side_reached_85"] += 1
                    continue
            at, side, observed, flipped = entry
            flush(at)
            if target_hit:
                missed["daily_target"] += 1
                continue
            price = min(.99, observed + slippage)
            stake = 30.0 if boosted(trades, at) else 25.0
            count = contracts_for_budget(stake, price)
            won = bool(s["won"]) if side == s["side"] else not bool(s["won"])
            net = count * (float(won) - price) - kalshi_fee_charged(price, count)
            trade = dict(day=day, window_open=s["wo"], signal_at=s["at"], entry_at=at,
                         signal_side=s["side"], entry_side=side, flipped=flipped,
                         observed_ask=observed, modeled_price=price, count=count,
                         stake=stake, won=won, net=round(net, 6),
                         known=s["wo"] + 960_000, booked=False)
            trades.append(trade)
        flush(float("inf"))
        days.append(dict(day=day, signals=len(rows), trades=len(trades),
                         wins=sum(t["won"] for t in trades), flips=sum(t["flipped"] for t in trades),
                         net=round(pnl, 4), max_dd=round(drawdown, 4),
                         target_hit=stamp(target_hit) if target_hit else "",
                         misses=dict(missed)))
        ledger.extend(trades)
    return days, ledger


def summary(days, trades):
    return dict(days=len(days), signals=sum(d["signals"] for d in days),
                trades=len(trades), wins=sum(t["won"] for t in trades),
                win_rate=round(100 * sum(t["won"] for t in trades) / len(trades), 2) if trades else 0,
                flips=sum(t["flipped"] for t in trades),
                net=round(sum(d["net"] for d in days), 2),
                worst_day=min((d["net"] for d in days), default=0),
                max_intraday_dd=max((d["max_dd"] for d in days), default=0),
                target_days=sum(bool(d["target_hit"]) for d in days),
                no_85=sum(d["misses"].get("neither_side_reached_85", 0) for d in days))


def main():
    signals, polls = load()
    scenarios = {}
    detailed = {}
    for mode in ("all_trades", "wait_85_after_loss", "wait_85_either_side"):
        for target in (False, True):
            for slip in (0.0, 0.01, 0.05):
                key = f"{mode}|{'3pct' if target else 'uncapped'}|slip{int(slip*100)}c"
                days, trades = replay(signals, polls, mode, slip, target)
                scenarios[key] = summary(days, trades)
                detailed[key] = (days, trades)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(scenarios, indent=2), encoding="utf-8")
    key = "wait_85_either_side|3pct|slip0c"
    days, trades = detailed[key]
    with (OUT / "daily.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=days[0]); w.writeheader(); w.writerows(days)
    with (OUT / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=trades[0]); w.writeheader(); w.writerows(trades)
    print(json.dumps(scenarios, indent=2))


if __name__ == "__main__":
    main()
