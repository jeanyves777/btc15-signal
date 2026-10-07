"""When two or three of OUR OWN signals fire in the same window, is the combo
better than taking them separately?

THE QUESTION, in the operator's words: use the exact signal, the same
requirement we use to trade, from when they are triggered to when they end, and
treat the simultaneous ones as a combo.

WHY THIS IS THE ONLY COMBO TEST WORTH RUNNING. Every earlier attempt here priced
arbitrary baskets - all-up, one-opposite, eight legs of whatever was quoted -
and every one of them was a bet on correlation alone. But this system does not
sell correlation. It sells a measured entry edge on a single instrument, and
that edge only exists on setups its gates admit. A combo built from two admitted
setups carries BOTH legs' edge AND the co-movement; a combo built from whatever
the app happens to show carries neither.

THE SIGNAL IS NOT REIMPLEMENTED HERE. This builds the real `KalshiBRTIRule` from
each instrument's deployed strategy_kalshi_*.json and calls the live
`check_facts`, then applies the same entry-window test as
`kalshi_signal.evaluate`. A reimplementation would be a different strategy
wearing the same name, which is exactly the error FINDINGS 77 is about. If the
deployed config changes, this follows it.

ONE SIGNAL PER WINDOW PER ASSET - the FIRST decision point that qualifies, which
is what the live loop takes. Later points in the same window are the same trade
re-observed, not new evidence, and counting them is the row-counting mistake
that produced the retracted FINDINGS 86.

PRICING. The combo's cost is the PRODUCT of the legs' asks, which is what the
combo orderbook quotes (FINDINGS 80: a resting bid filled at 0.0500 against a
product of 0.0456). The app/RFQ path charges more, so the product is the
best case and is reported as such. Fees are not modelled, per the operator's
standing instruction.

    python scripts/signal_combo.py
    python scripts/signal_combo.py --legs 2
"""

import argparse
import itertools
import json
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal.brti import BRTIFeatures  # noqa: E402
from btc15_signal.kalshi_brti import KalshiBRTIRule  # noqa: E402

# instrument -> (deployed config, brti corpus, market book)
INSTRUMENTS = {
    "BTC":  ("strategy_kalshi.json",       "brti_history.db",       "market_data.db"),
    "ETH":  ("strategy_kalshi_eth.json",   "brti_history_eth.db",   "market_data_kxeth15m.db"),
    "SOL":  ("strategy_kalshi_sol.json",   "brti_history_sol_merged.db", "market_data_kxsol15m.db"),
    "XRP":  ("strategy_kalshi_xrp.json",   "brti_history_xrp.db",   "market_data_kxxrp15m.db"),
    "NEAR": ("strategy_kalshi_near.json",  "brti_history_near.db",  "market_data_kxnear15m.db"),
}


def rule_from(cfg_path: Path, neutral_retrace: bool = False) -> KalshiBRTIRule:
    """The DEPLOYED rule, built from the same JSON the live service reads.

    `neutral_retrace` holds the reversal gate open. It is NOT the deployed rule
    and is only for BTC and ETH, whose corpora predate the brti_retrace column
    entirely: their configs leave `require_measurable_retrace` unset, so the
    dataclass default True applies and every archived row is - correctly -
    refused for a field that was never stored. Results obtained this way are
    labelled as a different rule wherever they appear, because a rule that
    drops a gate is a different strategy wearing the same name.
    """
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    fields = {f for f in KalshiBRTIRule.__dataclass_fields__}
    kw = {k: v for k, v in cfg.items() if k in fields}
    if neutral_retrace:
        kw["require_measurable_retrace"] = False
        kw["max_brti_retrace"] = 1.0
    return KalshiBRTIRule(**kw)


def book_of(market_db: Path) -> dict:
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    out = {}
    for t, ts, bid, ask in con.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
        if bid is None or ask is None:
            continue
        out[(t, ts * 1000 if ts < 1e11 else ts)] = (bid, ask)
    con.close()
    return out


def signals_for(asset: str, cfg_name: str, brti_name: str,
                market_name: str, neutral_retrace: bool = False):
    """{close_ms: signal} - the FIRST qualifying decision point per window."""
    cfg_path, brti_path = ROOT / cfg_name, ROOT / "data" / brti_name
    market_path = ROOT / "data" / market_name
    if not (cfg_path.exists() and brti_path.exists() and market_path.exists()):
        return None, None
    rule = rule_from(cfg_path, neutral_retrace)
    book = book_of(market_path)

    con = sqlite3.connect(f"file:{brti_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    have = {x[1] for x in con.execute("PRAGMA table_info(brti_decision_points)")}

    def col(name):
        return name if name in have else f"NULL AS {name}"

    sql = (
        "SELECT ticker, remaining_s, close_ms, result, brti_side, "
        "       signed_distance_bps, brti_momentum_bps, "
        f"      {col('brti_normalized_distance')}, {col('brti_accel')}, "
        f"      {col('brti_held_s')}, {col('brti_rejections')}, "
        f"      {col('brti_retrace')}, {col('brti_choppiness')}, "
        f"      {col('brti_volatility_bps')} "
        "  FROM brti_decision_points "
        " WHERE result IN ('yes','no') AND signed_distance_bps IS NOT NULL "
        " ORDER BY close_ms, remaining_s DESC")

    best, seen_windows = {}, set()
    considered = 0
    why = defaultdict(int)
    for r in con.execute(sql):
        close_ms = int(r["close_ms"])
        if close_ms in seen_windows:
            continue                      # already have this window's entry
        ms = close_ms - r["remaining_s"] * 1000
        quote = book.get((r["ticker"], ms // 60000 * 60000))
        if quote is None:
            continue
        yes_bid, yes_ask = quote
        yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
        yes_bid = yes_bid / 100 if yes_bid > 1 else yes_bid
        side = r["brti_side"]
        ask = yes_ask if side == "UP" else round(1 - yes_bid, 4)
        if not 0.02 <= ask <= 0.98:
            continue
        considered += 1
        feats = BRTIFeatures(
            event_ticker=r["ticker"], ts_ms=ms, target=0.0, value=0.0,
            signed_distance_bps=r["signed_distance_bps"],
            brti_momentum_bps=r["brti_momentum_bps"] or 0.0,
            brti_volatility_bps=r["brti_volatility_bps"] or 0.0,
            brti_normalized_distance=r["brti_normalized_distance"] or 0.0,
            samples=0, span_ms=0, stale=False, settlement_projection=0.0,
            brti_retrace=r["brti_retrace"],
            brti_choppiness=r["brti_choppiness"],
            brti_accel=r["brti_accel"], brti_held_s=r["brti_held_s"],
            brti_rejections=r["brti_rejections"])
        # THE LIVE TEST, not a copy of it.
        facts = rule.check_facts(feats, ask, r["remaining_s"])
        failed = [f["name"] for f in facts if not f["passed"]]
        in_window = (rule.entry_to_seconds <= r["remaining_s"]
                     <= rule.entry_from_seconds)
        if not in_window:
            why["entry window"] += 1
        for name in failed:
            why[name] += 1
        if failed or not in_window:
            continue
        seen_windows.add(close_ms)
        best[close_ms] = {
            "asset": asset, "ticker": r["ticker"], "side": side, "ask": ask,
            "won": (r["result"] == "yes") == (side == "UP"),
            "remaining_s": r["remaining_s"],
            "day": datetime.fromtimestamp(close_ms / 1000,
                                          timezone.utc).date(),
        }
    con.close()
    return best, (considered, dict(why))


def boot_p(vals, draws=4000, seed=13):
    """Two-sided day-clustered bootstrap p-value for mean != 0."""
    if not vals:
        return 1.0
    byday = defaultdict(list)
    for day, v in vals:
        byday[day].append(v)
    days = list(byday)
    obs = statistics.fmean([v for _, v in vals])
    centre = obs
    rng = random.Random(seed)
    hits = 0
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        pool = [v for d in pick for v in byday[d]]
        if not pool:
            continue
        # shift to the null: does a resample reach zero as easily as `obs`?
        if (statistics.fmean(pool) - centre) * (1 if obs > 0 else -1) <= -abs(obs):
            hits += 1
    return max((2.0 * hits) / draws, 1.0 / draws)


def boot(vals, draws=4000, seed=7):
    """Day-clustered bootstrap CI on the mean."""
    if not vals:
        return (0.0, 0.0, 0.0)
    byday = defaultdict(list)
    for day, v in vals:
        byday[day].append(v)
    days = list(byday)
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        pool = [v for d in pick for v in byday[d]]
        if pool:
            means.append(statistics.fmean(pool))
    means.sort()
    flat = [v for _, v in vals]
    return (statistics.fmean(flat),
            means[int(0.025 * len(means))],
            means[int(0.975 * len(means))])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legs", type=int, nargs="+", default=[2, 3])
    ap.add_argument("--min-n", type=int, default=25)
    ap.add_argument("--neutral-retrace", nargs="*", default=["BTC", "ETH"],
                    help="instruments whose corpus predates brti_retrace; "
                         "their gate is held open and the result is LABELLED "
                         "as a different rule")
    args = ap.parse_args()

    print("=" * 78)
    print("OUR OWN SIGNALS, TAKEN AS COMBOS")
    print("=" * 78)
    sig, skipped = {}, []
    for asset, (cfg, brti, mkt) in INSTRUMENTS.items():
        neutral = asset in (args.neutral_retrace or [])
        s, considered = signals_for(asset, cfg, brti, mkt, neutral)
        if s is None:
            skipped.append(asset)
            continue
        considered, why = considered
        sig[asset] = s
        days = len({v["day"] for v in s.values()})
        wr = statistics.fmean([1.0 if v["won"] else 0.0
                               for v in s.values()]) if s else 0.0
        ask = statistics.fmean([v["ask"] for v in s.values()]) if s else 0.0
        print(f"  {asset:<5} {len(s):>5} signals over {days:>3} days   "
              f"win {wr:>6.1%}   mean ask {ask:.3f}   "
              f"residual {wr - ask:+.4f}   (from {considered} priced points)"
              + ("   [retrace gate HELD OPEN - not the deployed rule]"
                 if neutral else ""))
        if not s and why:
            top = sorted(why.items(), key=lambda kv: -kv[1])[:5]
            print("        rejected by: " + ", ".join(
                f"{k} {v}" for k, v in top))
    if skipped:
        print(f"  skipped (no config or corpus): {', '.join(skipped)}")

    assets = sorted(sig)
    print("\n  THE SINGLE-LEG EDGE ABOVE IS WHAT A COMBO MUST BEAT.")
    print("  residual = win rate - ask = expected value per contract, no fees.")

    results = []
    for k in args.legs:
        for combo in itertools.combinations(assets, k):
            rows = []
            for close_ms in sig[combo[0]]:
                legs = [sig[a].get(close_ms) for a in combo]
                if any(l is None for l in legs):
                    continue
                cost = 1.0
                for l in legs:
                    cost *= l["ask"]
                allwin = all(l["won"] for l in legs)
                stake = sum(l["ask"] for l in legs)
                rows.append({
                    "day": legs[0]["day"], "cost": cost, "win": allwin,
                    "stake": stake,
                    # RETURN ON CAPITAL, which is the only fair comparison.
                    # A combo contract costs the product; one contract of each
                    # leg costs the SUM. Comparing the two per-contract makes
                    # the combo look better purely because it is cheaper.
                    "combo_roi": ((1.0 if allwin else 0.0) - cost) / cost,
                    "single_roi": sum((1.0 if l["won"] else 0.0) - l["ask"]
                                      for l in legs) / stake,
                })
            if len(rows) < args.min_n:
                continue
            mu, lo, hi = boot([(r["day"], r["combo_roi"]) for r in rows])
            smu, slo, shi = boot([(r["day"], r["single_roi"]) for r in rows])
            # PAIRED difference, same windows, so the comparison is within-day
            dmu, dlo, dhi = boot([(r["day"], r["combo_roi"] - r["single_roi"])
                                  for r in rows])
            pval = boot_p([(r["day"], r["combo_roi"] - r["single_roi"])
                           for r in rows])
            sd = (statistics.pstdev([r["combo_roi"] for r in rows])
                  if len(rows) > 1 else 0.0)
            ssd = (statistics.pstdev([r["single_roi"] for r in rows])
                   if len(rows) > 1 else 0.0)
            wr = statistics.fmean([1.0 if r["win"] else 0.0 for r in rows])
            cost = statistics.fmean([r["cost"] for r in rows])
            results.append({
                "legs": k, "name": "+".join(combo), "n": len(rows),
                "winrate": wr, "cost": cost,
                "roi": mu, "roi_lo": lo, "roi_hi": hi,
                "single": smu, "diff": dmu, "diff_lo": dlo, "diff_hi": dhi,
                # HOW MUCH OVER THE PRODUCT THIS CAN BE BOUGHT AND STILL BREAK
                # EVEN. The product is the orderbook's price; the app charges
                # more (FINDINGS 80), so this is the number that decides
                # whether the edge survives the venue.
                "breakeven_x": (wr / cost) if cost else 0.0,
                "p": pval, "sd": sd, "ssd": ssd,
                # return per unit of risk - a combo with 3x the return and 5x
                # the swing is not an improvement
                "ir": (mu / sd) if sd else 0.0,
                "sir": (smu / ssd) if ssd else 0.0,
            })

    # HOLM-BONFERRONI over every basket tested. Sixteen baskets searched for a
    # positive interval WILL produce positive intervals; this is the house
    # standard and the reason a starred row means something.
    m = len(results)
    ordered = sorted(results, key=lambda x: x["p"])
    still = True
    for i, r in enumerate(ordered):
        thresh = 0.05 / (m - i)
        r["holm_alpha"] = thresh
        r["holm"] = still and r["p"] <= thresh
        if not r["holm"]:
            still = False
    alive = [r for r in results if r["holm"]]

    for k in args.legs:
        print()
        print("=" * 78)
        print(f"  {k}-LEG COMBOS OF SIMULTANEOUS SIGNALS")
        print(f"{'=' * 78}")
        print(f"  {'basket':<16}{'n':>5}{'all-win':>9}{'cost':>7}"
              f"{'combo ROI':>11}{'singles':>9}{'diff':>9}"
              f"{'95% CI on diff':>20}{'b/e':>7}{'ret/risk':>10}")
        rows = [r for r in results if r["legs"] == k]
        if not rows:
            print(f"  no basket reached {args.min_n} co-occurring signals")
            continue
        for r in sorted(rows, key=lambda x: -x["diff"]):
            mark = "  HOLM" if r["holm"] else ""
            print(f"  {r['name']:<16}{r['n']:>5}{r['winrate']:>9.1%}"
                  f"{r['cost']:>7.3f}{r['roi']:>+11.1%}{r['single']:>+9.1%}"
                  f"{r['diff']:>+9.1%}  [{r['diff_lo']:+.1%},{r['diff_hi']:+.1%}]"
                  f"{r['breakeven_x']:>6.2f}x"
                  f"{r['ir']:>6.2f}/{r['sir']:.2f}{mark}")

    print()
    print("  combo ROI = return per DOLLAR STAKED on the combo (cost = product")
    print("              of the legs' asks, which is the combo orderbook price)")
    print("  singles   = return per dollar staked buying one contract of each")
    print("              leg separately, SAME windows - so this is paired")
    print("  diff      = combo minus singles, bootstrapped within day")
    print("  b/e       = most you can pay as a multiple of the product before")
    print("              the edge is gone. FINDINGS 80: the app/RFQ path")
    print("              charges well above the product, so a b/e under about")
    print("              1.3x means the edge does NOT survive that venue.")
    print()
    print("  ret/risk  = combo return per unit of its own standard deviation,")
    print("              then the same for singles. A combo that triples the")
    print("              return and quintuples the swing is not an improvement.")
    print()
    print(f"  {len(alive)} of {m} baskets survive Holm-Bonferroni at FWER 0.05")
    print("  on the PAIRED combo-minus-singles difference (marked HOLM).")

if __name__ == "__main__":
    main()
