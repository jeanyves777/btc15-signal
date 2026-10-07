"""Does the combo edge survive at REAL mid-window quoted prices?

`measure_combo_correlation.py` prices each leg at its marginal frequency (~50%),
which is the combo you could form at the window's OPEN. It found same-direction
three-leg combos happen 2.49x more often than the product of those prices.

But a combo is bought mid-window, at the prices the book is quoting THEN - the
operator's screenshot showed 83% / 72% / 66% with nine minutes left. Those prices
already contain the move so far, and the legs have already co-moved to get there.
So the open-window result does not transfer automatically: the remaining
uncertainty might be far less correlated than the whole window's.

This measures the real thing. For each 15-minute window and each decision minute
this system archives, it takes the QUOTED ask for each asset's UP side, forms the
all-UP and all-DOWN combos, prices them as the product of those quotes - which is
how the exchange prices them - and compares against what actually settled.

    edge = actual joint frequency / product of quoted probabilities

Above 1.00 means the quoted product underprices the combo.

No fees, per the operator's standing instruction. A real combo pays fees per leg
and crosses a spread on each, both additive costs this does not model.
"""

import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# THE CANDLE BOOK, NOT THE DECISION CORPORA. The brti_history_* corpora are
# STRIDE-SAMPLED - each was built by walking a different market list - so they
# share almost no (window, minute) keys and intersecting five of them yields
# nothing. `markets` + `contract_candles` cover EVERY market, which is what a
# cross-asset comparison needs.
MARKETS = {
    "BTC": "market_data.db",
    "ETH": "market_data_kxeth15m.db",
    "SOL": "market_data_kxsol15m.db",
    "XRP": "market_data_kxxrp15m.db",
    "NEAR": "market_data_kxnear15m.db",
}


def quoted(asset, market_db):
    """{(close_ms, minutes_before_close): (p_up, rose)} straight from the book."""
    mp = ROOT / "data" / market_db
    if not mp.exists():
        return {}
    con = sqlite3.connect(f"file:{mp}?mode=ro", uri=True)
    result = {}
    for ticker, close_ms, res in con.execute(
            "SELECT ticker, close_ms, result FROM markets "
            " WHERE result IN ('yes','no') AND floor_strike IS NOT NULL"):
        result[ticker] = (int(close_ms), res == "yes")
    out = {}
    for ticker, ts, bid, ask in con.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
        if bid is None or ask is None or ticker not in result:
            continue
        close_ms, rose = result[ticker]
        ms = ts * 1000 if ts < 1e11 else ts
        minutes = round((close_ms - ms) / 60000)
        if not 1 <= minutes <= 14:
            continue
        yes_bid = bid / 100 if bid > 1 else bid
        yes_ask = ask / 100 if ask > 1 else ask
        p_up = (yes_bid + yes_ask) / 2
        if not 0.02 <= p_up <= 0.98:
            continue
        out[(close_ms, minutes)] = (p_up, rose)
    con.close()
    return out


def main():
    print("=" * 76)
    print("DOES THE COMBO EDGE SURVIVE AT REAL MID-WINDOW QUOTES?")
    print("=" * 76)
    data = {}
    for a, mdb in MARKETS.items():
        d = quoted(a, mdb)
        data[a] = d
        print(f"  {a:<5} {len(d)} quoted decision points")

    assets = [a for a in MARKETS if data.get(a)]
    # windows+minutes where EVERY asset has a live quote
    keys = set.intersection(*(set(data[a]) for a in assets)) if assets else set()
    print(f"\n{len(keys)} (window, minute) points where all {len(assets)} "
          f"assets were quoted at once")
    if len(keys) < 200:
        print("  too few to conclude")
        return

    by_minute = defaultdict(list)
    for k in keys:
        by_minute[k[1]].append(k)

    print(f"\n  {'minute':<9}{'n':>6}{'ALL-UP':>9}{'product':>10}{'lift':>8}"
          f"{'ALL-DOWN':>10}{'product':>10}{'lift':>8}")
    all_lifts = []
    for remaining in sorted(by_minute, reverse=True):
        ks = by_minute[remaining]
        if len(ks) < 100:
            continue
        up_prod = down_prod = 0.0
        up_hit = down_hit = 0
        for k in ks:
            pu = 1.0
            pd = 1.0
            rose_all = True
            fell_all = True
            for a in assets:
                p, rose = data[a][k]
                pu *= p
                pd *= (1 - p)
                rose_all = rose_all and rose
                fell_all = fell_all and not rose
            up_prod += pu
            down_prod += pd
            up_hit += 1 if rose_all else 0
            down_hit += 1 if fell_all else 0
        n = len(ks)
        up_e, down_e = up_prod / n, down_prod / n
        up_a, down_a = up_hit / n, down_hit / n
        lu = up_a / up_e if up_e else 0
        ld = down_a / down_e if down_e else 0
        all_lifts += [lu, ld]
        print(f"  {remaining:<9}{n:>6}{up_a:>9.2%}{up_e:>10.2%}{lu:>7.2f}x"
              f"{down_a:>10.2%}{down_e:>10.2%}{ld:>7.2f}x")

    if all_lifts:
        print(f"\n  MEAN LIFT across minutes, same-direction {len(assets)}-leg:"
              f" {statistics.mean(all_lifts):.2f}x")
        print("  Above 1.00x: the product of the QUOTED prices underprices the")
        print("  combo, which is the price the exchange actually charges.")


if __name__ == "__main__":
    main()
