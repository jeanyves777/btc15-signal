"""Build the comparable-market corpus the similarity layer retrieves from.

The operator's specification: at every poll, fingerprint the current market,
retrieve SIMILAR historical lifecycles rather than exact matches, and compute
what actually happened to them - win rate, net edge, whether a better price
appeared later, whether the opportunity ran away.

That retrieval has to be fast and it must never touch the order path, so the
corpus is precomputed here into one table and simply read at decision time.

Each row is one decision minute of one settled market, carrying both the
fingerprint and, crucially, WHAT HAPPENED NEXT - which is knowable in history
and is the whole point:

    best_later_ask   the cheapest ask OUR side still showed before it shut
    ran_away         our side's ask left the band upward and stayed there
    won              the settlement

Every forward-looking field follows ONE instrument - the side the decision
row was about - for the whole remainder of that window.

The forward-looking fields are legitimate HERE because every row is a settled
market from the past. They are what the live decision gets to learn from. The
live row being scored never contributes its own future to its own answer.

    python scripts/build_cohort.py
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_series import load  # noqa: E402

from btc15_signal.features import _session, build_snapshots  # noqa: E402

OUT = "data/cohort.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS cohort (
    ticker TEXT, open_ms INTEGER, remaining INTEGER,
    hour_utc INTEGER, session TEXT,
    ask REAL, side_is_up INTEGER,
    distance_bps REAL, normalized_distance REAL,
    momentum_bps REAL, volatility_bps REAL, vol_regime TEXT,
    best_later_ask REAL,      -- cheapest ask OUR side showed later
    last_ask REAL,            -- our side's ask at the final qualifying minute
    ran_away INTEGER,         -- our side stayed above the band to the end
    won INTEGER,
    PRIMARY KEY (ticker, remaining)
);
CREATE INDEX IF NOT EXISTS cohort_lookup ON cohort(remaining, vol_regime);
"""


def main() -> None:
    markets, klines, candles = load("data/market_data.db")
    if not klines:
        print("no klines - run scripts/fetch_klines.py first")
        return
    snapshots = build_snapshots(markets, klines, candles)

    by_market: dict[str, list] = {}
    for snap in snapshots:
        by_market.setdefault(snap.ticker, []).append(snap)

    Path("data").mkdir(exist_ok=True)
    db = sqlite3.connect(OUT)
    db.executescript(SCHEMA)
    db.execute("DELETE FROM cohort")

    rows = []
    for ticker, snaps in by_market.items():
        snaps.sort(key=lambda s: -s.remaining)
        if snaps[0].result not in ("yes", "no"):
            continue
        quotes = []
        for snap in snaps:
            if snap.yes_ask is None or snap.yes_bid is None:
                continue
            up, down = snap.yes_ask, 1 - snap.yes_bid
            side_is_up = up >= down
            ask = max(up, down)
            if not 0 < ask < 1:
                continue
            won = (snap.result == "yes") if side_is_up else (snap.result == "no")
            # Carry BOTH prices, not just the favourite's: the forward columns
            # below have to follow one fixed instrument, and which one that is
            # depends on the decision row, not on this minute.
            quotes.append((snap, ask, side_is_up, won, up, down))
        if not quotes:
            continue
        for index, (snap, ask, side_is_up, won, _up, _down) in enumerate(quotes):
            # OUR side, fixed at this row, for the rest of the window. These
            # used to read the per-minute max(yes_ask, 1 - yes_bid), which is
            # whichever side is the favourite THAT minute - and the favourite
            # flips every time BTC crosses the strike, so a row's "later ask"
            # was often the price of the opposite contract. mean_drift, the
            # retracement fill test and ran_away were then differencing two
            # different instruments.
            later = [u if side_is_up else d
                     for _s, _a, _si, _w, u, d in quotes[index + 1:]]
            best_later = min(later, default=ask)
            last_ask = later[-1] if later else ask
            # "Ran away" in the operator's sense: the opportunity disappeared
            # because OUR side's ask left the tradeable band upward and stayed
            # there. With the side fixed that is one-directional on purpose:
            # our side collapsing toward 0 is the market going against us, not
            # the price running away, and it can no longer reach 0.97 wearing
            # the opposite contract's price.
            ran_away = int(bool(later) and best_later > 0.97)
            rows.append((
                ticker, snap.open_ms, snap.remaining,
                (snap.open_ms // 3_600_000) % 24, _session(snap.open_ms),
                round(ask, 4), int(side_is_up),
                round(snap.signed_distance_bps, 2),
                round(abs(snap.signed_distance_bps)
                      / max(snap.volatility_5m_bps, 1.0), 3),
                round(snap.momentum_5m_bps, 2),
                round(snap.volatility_5m_bps, 3),
                ("low" if snap.volatility_5m_bps < 8
                 else "high" if snap.volatility_5m_bps > 20 else "mid"),
                round(best_later, 4), round(last_ask, 4), ran_away, int(won),
            ))

    db.executemany(
        "INSERT OR REPLACE INTO cohort VALUES (" + ",".join("?" * 16) + ")", rows
    )
    db.commit()
    span = db.execute("SELECT COUNT(*), COUNT(DISTINCT ticker) FROM cohort").fetchone()
    print(f"cohort built: {span[0]} decision minutes across {span[1]} settled markets")
    print(f"  -> {OUT}")
    for regime in ("low", "mid", "high"):
        n = db.execute(
            "SELECT COUNT(*) FROM cohort WHERE vol_regime=?", (regime,)
        ).fetchone()[0]
        print(f"  {regime:>5} volatility: {n}")


if __name__ == "__main__":
    main()
