"""Fit a whole gate SET jointly, under brti-4, with volume as a constraint.

THE ERROR THIS REPAIRS, made 2026-09-25. Silver and SOL each received seven
thresholds and every one was picked from its own best bucket in isolation:
price 0.50-0.60, distance <=2bp, held >=60s, rejections >=10, momentum <=10bp,
accel -10..+10, plus an inherited retrace gate. Each was defensible alone.
Intersected they admitted NOTHING - 423 and 422 live evaluations, zero
qualified, every market blocked - and the check that would have caught it, does
the COMBINATION admit a usable share, was never run. Gold qualified 18.7% on
two active gates.

Three rules follow from that, and this script enforces all three.

  1. SEARCH SETS, NOT GATES. A gate is only ever scored inside a complete
     candidate configuration, so the reported number is the number the live
     service will produce.

  2. VOLUME IS A CONSTRAINT. A set admitting less than --min-share cannot be
     learned from: the confidence arms never reach `min_evidence`, the learned
     layer stays inert, and the configuration can then never be corrected by
     evidence. A weaker edge that produces data beats a stronger one that
     produces none.

  3. A GATE MAY NOT SELECT FOR LOSERS. Every candidate must score at least the
     ungated residual. This is what "don't just block or let through bad
     setups" means arithmetically: gating that lands below the baseline is
     choosing worse setups than taking everything.

Ladders WIDEN, they never disable. Every rung is a real threshold that some
observed value fails, so no gate becomes a check nothing can fail - the
rendering that gold's audit rejected.

Scored on the calibration residual (win rate - price paid), day-clustered
bootstrap, no fees, per the operator's standing instruction.
"""

import argparse
import itertools
import json
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NL = chr(10)


# --------------------------------------------------------------- corpus load

def load(brti: str, market: str):
    m = sqlite3.connect(f"file:{market}?mode=ro", uri=True)
    book = {}
    for t, ts, bid, ask in m.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
        if bid is None or ask is None:
            continue
        book[(t, ts * 1000 if ts < 1e11 else ts)] = (bid, ask)
    m.close()
    b = sqlite3.connect(f"file:{brti}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    # The older corpora predate these two columns. Selecting NULL keeps one
    # load path for both rather than a second reader that could drift.
    have = {x[1] for x in b.execute("PRAGMA table_info(brti_decision_points)")}
    retr = ("brti_retrace" if "brti_retrace" in have
            else "NULL AS brti_retrace")
    chop = ("brti_choppiness" if "brti_choppiness" in have
            else "NULL AS brti_choppiness")
    rows = []
    for r in b.execute(
            "SELECT ticker, remaining_s, close_ms, result, brti_side, "
            "       signed_distance_bps sd, brti_momentum_bps mom, "
            "       brti_normalized_distance nd, brti_accel, brti_held_s, "
            f"       brti_rejections, {retr}, {chop} "
            "  FROM brti_decision_points "
            " WHERE result IN ('yes','no') AND signed_distance_bps IS NOT NULL"):
        ms = r["close_ms"] - r["remaining_s"] * 1000
        q = book.get((r["ticker"], ms // 60000 * 60000))
        if q is None:
            continue
        yes_bid, yes_ask = q
        yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
        no_ask = round(1 - (yes_bid / 100 if yes_bid > 1 else yes_bid), 4)
        ask = yes_ask if r["brti_side"] == "UP" else no_ask
        if not 0.02 <= ask <= 0.98:
            continue
        rows.append({
            "ticker": r["ticker"],
            "won": (r["result"] == "yes") == (r["brti_side"] == "UP"),
            "ask": ask, "gap": abs(r["sd"]), "mom": abs(r["mom"] or 0.0),
            "nd": r["nd"], "accel": r["brti_accel"], "held": r["brti_held_s"],
            "rej": r["brti_rejections"], "rem": r["remaining_s"],
            "retrace": r["brti_retrace"], "chop": r["brti_choppiness"],
            "day": datetime.fromtimestamp(
                r["close_ms"] / 1000, timezone.utc).date(),
        })
    b.close()
    return rows


# ------------------------------------------------------------------- scoring

def _byday(sel):
    byday = defaultdict(list)
    for r in sel:
        byday[r["day"]].append((1.0 if r["won"] else 0.0) - r["ask"])
    return byday


def score(sel, draws=3000, seed=11, min_n=60, min_days=6):
    """Day-clustered bootstrap. Days are the unit because points inside one
    day share the same price path - treating them as independent is how a
    single good afternoon becomes a 'significant' edge."""
    if len(sel) < min_n:
        return None
    byday = _byday(sel)
    days = list(byday)
    if len(days) < min_days:
        return None
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        vals = [x for d in pick for x in byday[d]]
        out.append(sum(vals) / len(vals))
    out.sort()
    flat = [x for v in byday.values() for x in v]
    lo = out[int(draws * 0.025)]
    hi = out[int(draws * 0.975) - 1]
    return {"mu": statistics.mean(flat), "lo": lo, "hi": hi, "n": len(sel),
            "win": sum(1 for r in sel if r["won"]) / len(sel),
            "days": len(days),
            "markets": len({r["ticker"] for r in sel}),
            "ask": statistics.mean(r["ask"] for r in sel)}


def delta(sel, allrows, draws=3000, seed=11):
    """Does gating BEAT NOT GATING? Paired on days, so the same resample scores
    both sides and the comparison is not two independent intervals eyeballed
    against each other.

    This is the filter that would have caught the degenerate answer as well as
    the empty one. A set admitting 74% of points scores fractionally over the
    ungated baseline, and without this test that looks like an improvement -
    it is the baseline with extra steps, and it selects for nothing.
    """
    g, a = _byday(sel), _byday(allrows)
    days = [d for d in a if d in g]
    if len(days) < 6:
        return None
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        gv = [x for d in pick for x in g[d]]
        av = [x for d in pick for x in a[d]]
        if not gv or not av:
            continue
        out.append(sum(gv) / len(gv) - sum(av) / len(av))
    if len(out) < draws // 2:
        return None
    out.sort()
    flat_g = [x for v in g.values() for x in v]
    flat_a = [x for v in a.values() for x in v]
    return {"mu": statistics.mean(flat_g) - statistics.mean(flat_a),
            "lo": out[int(len(out) * 0.025)],
            "hi": out[int(len(out) * 0.975) - 1]}


def halves(sel, draws=3000):
    """Disjoint sub-periods. Gold shipped only after both halves held
    independently; silver and SOL were shipped on a whole-sample number and
    both decayed in their second half."""
    days = sorted({r["day"] for r in sel})
    if len(days) < 12:
        return None, None
    cut = days[len(days) // 2]
    a = score([r for r in sel if r["day"] < cut], draws=draws, min_days=4)
    b = score([r for r in sel if r["day"] >= cut], draws=draws, min_days=4)
    return a, b


# ------------------------------------------------------------ the gate ladder

def admits(r, c):
    if not c["price"][0] <= r["ask"] <= c["price"][1]:
        return False
    if not c["gap"][0] <= r["gap"] <= c["gap"][1]:
        return False
    if r["mom"] > c["mom"]:
        return False
    if r["accel"] is not None and not -c["accel"] <= r["accel"] <= c["accel"]:
        return False
    if r["held"] is not None and r["held"] < c["held"]:
        return False
    if r["rej"] is not None and r["rej"] < c["rej"]:
        return False
    if not c["win"][1] <= r["rem"] <= c["win"][0]:
        return False
    # Mirrors the live rule exactly, including how it treats an unmeasurable
    # retrace - which is the whole point of having it here.
    cap, require = c["retrace"]
    if r["retrace"] is None:
        if require:
            return False
    elif r["retrace"] > cap:
        return False
    return True


LADDERS = {
    # Bands the instrument is actually quoted at. The widest rung still
    # excludes the tails, where a binary is near-decided either way.
    "price": [(0.55, 0.75), (0.60, 0.80), (0.65, 0.85), (0.60, 0.90),
              (0.55, 0.90)],
    # Absolute bps. Gold and silver pay at the CLOSE distances, BTC at the far
    # ones, so both ends of the band are candidates rather than a floor only.
    "gap": [(0.0, 4.0), (2.0, 5.0), (1.5, 6.0), (0.0, 8.0), (0.5, 12.0),
            (0.0, 25.0)],
    "mom": [10.0, 20.0, 40.0],
    "accel": [10.0, 25.0, 60.0],
    "held": [10.0, 30.0, 90.0],
    "rej": [1, 2, 4],
    # (900, 120) is dropped: decision points only exist at 780..120s, so it
    # scored identically to (800, 250) on every instrument and only doubled the
    # search.
    "win": [(700, 400), (800, 250)],
    # (cap, require_measurable). The live rule refuses an unmeasurable retrace
    # when the second is True. BTC's inherited default was (0.60, True) and it
    # was never measured on any of these three.
    "retrace": [(0.40, False), (0.60, False), (0.90, False), (0.60, True)],
}


def candidates():
    keys = list(LADDERS)
    for combo in itertools.product(*(LADDERS[k] for k in keys)):
        yield dict(zip(keys, combo))


def describe(c):
    return (f"price {c['price'][0]:.2f}-{c['price'][1]:.2f}  "
            f"gap {c['gap'][0]:.1f}-{c['gap'][1]:.1f}bp  "
            f"mom<={c['mom']:.0f}  |accel|<={c['accel']:.0f}  "
            f"held>={c['held']:.0f}s  rej>={c['rej']}  "
            f"win {c['win'][0]}-{c['win'][1]}s  "
            f"retr<={c['retrace'][0]:.2f}"
            f"{'/req' if c['retrace'][1] else ''}")


# ------------------------------------------------------ the deployed config

def deployed(name):
    path = ROOT / f"strategy_kalshi_{name}.json"
    if not path.exists():
        return None
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return {
        "price": (cfg.get("min_ask", 0.0), cfg.get("max_ask", 1.0)),
        "gap": (cfg.get("min_abs_distance_bps") or 0.0,
                cfg.get("max_abs_distance_bps") or 1e9),
        "mom": cfg.get("max_brti_momentum_bps") or 1e9,
        "accel": abs(cfg.get("max_brti_accel") or 1e9),
        "held": cfg.get("min_brti_held_s") or 0.0,
        "rej": cfg.get("min_brti_rejections") or 0,
        "win": (cfg.get("entry_from_seconds", 900),
                cfg.get("entry_to_seconds", 0)),
        "retrace": (cfg.get("max_brti_retrace", 0.60),
                    bool(cfg.get("require_measurable_retrace", True))),
    }


# ----------------------------------------------------------------- reporting

def marginal(rows, label, key, edges):
    print(f"\n  {label}")
    print(f"    {'bucket':<16}{'n':>6}{'win%':>8}{'residual':>10}{'95% CI':>22}")
    for lo, hi, name in edges:
        sel = [r for r in rows if r.get(key) is not None and lo <= r[key] < hi]
        s = score(sel, draws=1500)
        if s is None:
            print(f"    {name:<16}n={len(sel):<5} too few to score")
            continue
        print(f"    {name:<16}{s['n']:>6}{s['win']:>8.1%}{s['mu']:>+10.4f}"
              f"  [{s['lo']:+.4f},{s['hi']:+.4f}]"
              f"{'  HOLDS' if s['lo'] > 0 else ''}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--asset", required=True)
    p.add_argument("--brti", required=True)
    p.add_argument("--market", required=True)
    p.add_argument("--min-share", type=float, default=0.15,
                   help="a set admitting less than this cannot be learned from")
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--retrace", choices=("search", "neutral"),
                   default="search",
                   help="neutral holds retrace open, for a corpus where only "
                        "part of the rows carry it")
    p.add_argument("--retrace-sweep", default=None,
                   help="after choosing, sweep the retrace options on this "
                        "corpus with the chosen set fixed")
    p.add_argument("--crosscheck", default=None,
                   help="a second corpus to re-score the chosen set on, "
                        "retrace ignored because it is not stored there")
    args = p.parse_args()

    if args.retrace == "neutral":
        LADDERS["retrace"] = [(0.90, False)]
    rows = load(args.brti, args.market)
    if not rows:
        raise SystemExit(f"{args.asset}: no priced rows")
    base = score(rows, draws=6000)
    days = len({r["day"] for r in rows})
    print("=" * 78)
    print(f"{args.asset.upper()}  brti-4 (45-minute lookback)   "
          f"{len(rows)} priced points, "
          f"{len({r['ticker'] for r in rows})} markets, {days} days")
    print("=" * 78)
    print(f"  UNGATED BASELINE   n={base['n']}  win {base['win']:.1%}  "
          f"mean ask {base['ask']:.3f}  residual {base['mu']:+.4f} "
          f"[{base['lo']:+.4f},{base['hi']:+.4f}]")
    print("  Every candidate below must beat this. A gate set scoring under the")
    print("  baseline is selecting worse setups than taking everything.")

    dep = deployed(args.asset.lower())
    if dep:
        sel = [r for r in rows if admits(r, dep)]
        print(f"\n  DEPLOYED NOW        {describe(dep)}")
        s = score(sel, draws=6000)
        print(f"    admits {len(sel)}/{len(rows)} = {len(sel)/len(rows):.2%}"
              + ("  -- CANNOT BE LEARNED FROM" if len(sel) / len(rows)
                 < args.min_share else ""))
        if s:
            print(f"    win {s['win']:.1%}  residual {s['mu']:+.4f} "
                  f"[{s['lo']:+.4f},{s['hi']:+.4f}]")
        else:
            print("    too few points to score at all - this is the defect")

    marginal(rows, "price paid (marginal, for the record)", "ask",
             [(0.40, 0.55, "0.40-0.55"), (0.55, 0.65, "0.55-0.65"),
              (0.65, 0.75, "0.65-0.75"), (0.75, 0.85, "0.75-0.85"),
              (0.85, 0.99, "0.85+")])
    marginal(rows, "distance from strike, bps", "gap",
             [(0.0, 2.0, "< 2"), (2.0, 5.0, "2-5"), (5.0, 10.0, "5-10"),
              (10.0, 1e9, "10+")])
    marginal(rows, "level tested (rejections), 45-min window", "rej",
             [(0, 1, "0"), (1, 2, "1"), (2, 4, "2-3"), (4, 8, "4-7"),
              (8, 1e9, "8+")])
    marginal(rows, "level held, seconds", "held",
             [(0, 30, "< 30"), (30, 90, "30-90"), (90, 240, "90-240"),
              (240, 1e9, "240+")])
    # The None group is a bucket, not missing data: it means the recent window
    # held no advance to give back. Whether that is a warning or the target
    # state is exactly what the live `require_measurable_retrace` decides, so
    # it is scored as its own row.
    unmeasured = [r for r in rows if r["retrace"] is None]
    if unmeasured:
        u = score(unmeasured, draws=1500)
        print(f"{NL}  retrace unmeasurable (no advance to give back)")
        if u:
            print(f"    {'None':<16}{u['n']:>6}{u['win']:>8.1%}"
                  f"{u['mu']:>+10.4f}  [{u['lo']:+.4f},{u['hi']:+.4f}]"
                  f"{'  HOLDS' if u['lo'] > 0 else ''}")
        else:
            print(f"    n={len(unmeasured)} too few to score")
        print(f"    {len(unmeasured)/len(rows):.1%} of all points - refusing "
              f"these is what BTC's inherited default does")
    marginal(rows, "retrace, share of the move given back", "retrace",
             [(0.0, 0.2, "< 0.20"), (0.2, 0.4, "0.20-0.40"),
              (0.4, 0.6, "0.40-0.60"), (0.6, 1.01, "0.60+")])
    marginal(rows, "choppiness (confidence-only, never gated)", "chop",
             [(0.0, 0.3, "< 0.30"), (0.3, 0.5, "0.30-0.50"),
              (0.5, 0.7, "0.50-0.70"), (0.7, 1.01, "0.70+")])
    marginal(rows, "acceleration", "accel",
             [(-1e9, -10, "< -10"), (-10, 0, "-10..0"), (0, 10, "0..+10"),
              (10, 1e9, "+10 and up")])

    # ---------------------------------------------------------- joint search
    #
    # THE BAR IS THE ONE GOLD WAS SHIPPED ON: both disjoint halves holding
    # independently. Silver and SOL were shipped on a whole-sample number and
    # both decayed in the second half - the same signature that reversed
    # FINDINGS 63, 64, 66 and 68. Ranking on the whole-sample lower bound
    # reproduces that, so the tiers below are evaluated in order and a lower
    # tier is reported only when the one above it is empty.
    #
    #   A  volume + beats baseline + whole sample holds + BOTH halves hold
    #   B  volume + beats baseline + whole sample holds, one half decays
    #   C  volume + beats baseline only                    data capture only
    #
    # Within a tier the ranking is the LOWER BOUND ON "gating beats not
    # gating" - the strength of the evidence that the gates earn their place.
    # Residual alone prefers a narrow set that cannot be learned from, share
    # alone prefers a wide set that selects for nothing, and total edge
    # (n * residual) turned out to prefer the wide one too. The volume floor
    # already guarantees learnability, so the ranking only has to answer
    # whether the gating is worth having. Total edge is still reported.
    sets = list(candidates())
    print(f"{NL}  JOINT SEARCH over {len(sets)} complete sets, volume floor "
          f"{args.min_share:.0%}, must beat baseline {base['mu']:+.4f}")
    keep = []
    for c in sets:
        sel = [r for r in rows if admits(r, c)]
        share = len(sel) / len(rows)
        if share < args.min_share:
            continue
        s = score(sel, draws=400, seed=7)
        if s is None or s["mu"] < base["mu"]:
            continue
        # Ordered by residual, not total edge: total edge alone floods the
        # shortlist with near-ungated sets and the real candidates never reach
        # the properly-scored pass below.
        keep.append((s["mu"], share, c))
    print(f"    {len(keep)} sets clear volume and the baseline")
    if not keep:
        print("    NOTHING clears both. Report that rather than lowering the")
        print("    floor - a set chosen without a volume check is exactly how")
        print("    seven marginal winners became a config admitting nothing.")
        return

    # Re-score the plausible front properly, then tier. The coarse pass above
    # uses 400 draws only to order candidates; no threshold is set from it.
    keep.sort(key=lambda x: -x[0])
    graded = []
    for _, share, c in keep[:120]:
        sel = [r for r in rows if admits(r, c)]
        s = score(sel, draws=6000)
        if s is None:
            continue
        d = delta(sel, rows)
        a, b = halves(sel)
        both = bool(a and b and a["lo"] > 0 and b["lo"] > 0)
        # Three hard requirements, then tier on the halves.
        #   the set itself holds        s["lo"] > 0
        #   gating beats not gating     d["lo"] > 0
        #   enough volume to learn      already filtered above
        adds = bool(d and d["lo"] > 0)
        if s["lo"] > 0 and adds:
            tier = "A" if both else "B"
        else:
            tier = "C"
        graded.append({"c": c, "s": s, "a": a, "b": b, "share": share,
                       "tier": tier, "total": s["mu"] * s["n"], "d": d})

    best = None
    for tier in ("A", "B", "C"):
        pool = sorted((g for g in graded if g["tier"] == tier),
                      key=lambda g: (-(g["d"]["lo"] if g["d"] else -9),
                                     -g["total"]))
        if not pool:
            continue
        name = {"A": "TIER A - holds, beats ungated, and BOTH halves hold",
                "B": "TIER B - holds and beats ungated, one half decays"
                     "  (PROVISIONAL)",
                "C": "TIER C - does NOT demonstrably beat taking everything"
                     "  (DATA CAPTURE ONLY)"}[tier]
        print(f"{NL}  {name}   {len(pool)} of {len(graded)} scored")
        print(f"    {'total':>7}{'residual':>10}{'95% CI':>21}{'share':>8}"
              f"{'n':>6}{'win%':>7}{'vs ungated':>19}{'halves':>17}   set")
        for g in pool[:args.top]:
            s = g["s"]
            h = "  ".join(f"{x['mu']:+.3f}{'*' if x['lo'] > 0 else ' '}"
                          if x else "  n/a " for x in (g["a"], g["b"]))
            d = g["d"]
            dd = (f"{d['mu']:+.4f}[{d['lo']:+.3f}]" if d else "n/a")
            print(f"    {g['total']:>7.1f}{s['mu']:>+10.4f}  "
                  f"[{s['lo']:+.4f},{s['hi']:+.4f}]{g['share']:>8.1%}"
                  f"{s['n']:>6}{s['win']:>7.1%}{dd:>19}{h:>17}   "
                  f"{describe(g['c'])}")
        # TIEBREAK ON VOLUME, stated rather than eyeballed. The top sets'
        # delta lower bounds sit within a few thousandths of each other, which
        # is noise at 6,000 draws; choosing the highest is false precision.
        # Among the ones that are statistically indistinguishable from the
        # best, take the most volume - the same argument that put a floor
        # there in the first place, since a wider set reaches `min_evidence`
        # sooner and can therefore be corrected by evidence sooner.
        span = pool[0]["d"]["lo"] - 0.005 if pool[0]["d"] else -9
        tied = [g for g in pool if g["d"] and g["d"]["lo"] >= span]
        best = max(tied, key=lambda g: g["share"]) if tied else pool[0]
        if tied and best is not tied[0]:
            print(f"    tiebreak: {len(tied)} sets within 0.005 of the best "
                  f"lower bound; took the widest at {best['share']:.1%}")
        break

    c, s, a, b, share = (best["c"], best["s"], best["a"], best["b"],
                         best["share"])
    print(f"{NL}  CHOSEN (tier {best['tier']})   {describe(c)}")
    print(f"    n={s['n']} over {s['days']} days, {s['markets']} markets, "
          f"{share:.1%} of points, total edge {best['total']:.1f}")
    print(f"    win {s['win']:.1%} at mean ask {s['ask']:.3f}  "
          f"residual {s['mu']:+.4f} [{s['lo']:+.4f},{s['hi']:+.4f}]"
          + ("  HOLDS" if s["lo"] > 0 else "  SPANS ZERO"))
    if best["d"]:
        d = best["d"]
        print(f"    vs taking everything  {d['mu']:+.4f} "
              f"[{d['lo']:+.4f},{d['hi']:+.4f}]"
              + ("  gating adds" if d["lo"] > 0
                 else "  NOT demonstrably better than ungated"))
    for tag, x in (("first half", a), ("second half", b)):
        if x:
            print(f"    {tag:<12} n={x['n']:<5} {x['win']:>5.1%}  "
                  f"{x['mu']:+.4f} [{x['lo']:+.4f},{x['hi']:+.4f}]"
                  + ("  holds" if x["lo"] > 0 else "  spans zero"))
    if best["tier"] != "A":
        print(f"    TIER {best['tier']} must be recorded in the config as "
              f"PROVISIONAL. Silver and SOL were")
        print("    shipped without that and were read later as established.")
    if args.retrace_sweep:
        # STAGE 2. The set is fixed; only the retrace gate moves. Every option
        # is re-checked against volume, the ungated comparison and the halves,
        # because "the best retrace bucket" chosen on its own is precisely how
        # seven marginal winners became a config that admitted nothing.
        sweep = load(args.retrace_sweep, args.market)
        if sweep:
            base2 = score(sweep, draws=6000)
            print(f"{NL}  STAGE 2 - retrace, on {args.retrace_sweep}")
            print(f"    that corpus ungated: n={base2['n']} "
                  f"{base2['win']:.1%} {base2['mu']:+.4f}")
            unmeasurable = sum(1 for r in sweep if r["retrace"] is None)
            print(f"    retrace unmeasurable on {unmeasurable}/{len(sweep)} = "
                  f"{unmeasurable/len(sweep):.1%} of points")
            print(f"    {'retrace':<14}{'n':>6}{'share':>8}{'win%':>7}"
                  f"{'residual':>10}{'95% CI':>21}{'vs ungated':>19}"
                  f"{'halves':>17}")
            for cap, req in [(0.40, False), (0.60, False), (0.90, False),
                             (0.60, True)]:
                trial = dict(c)
                trial["retrace"] = (cap, req)
                sel = [r for r in sweep if admits(r, trial)]
                st = score(sel, draws=6000)
                if st is None:
                    print(f"    {f'<={cap}{chr(47)}req' if req else f'<={cap}':<14}"
                          f"{len(sel):>6}  too few to score")
                    continue
                dt = delta(sel, sweep)
                a2, b2 = halves(sel)
                h2 = "  ".join(f"{x['mu']:+.3f}{'*' if x['lo'] > 0 else ' '}"
                               if x else "  n/a " for x in (a2, b2))
                dd = f"{dt['mu']:+.4f}[{dt['lo']:+.3f}]" if dt else "n/a"
                label = f"<={cap:.2f}" + ("/req" if req else "")
                print(f"    {label:<14}{st['n']:>6}{len(sel)/len(sweep):>8.1%}"
                      f"{st['win']:>7.1%}{st['mu']:>+10.4f}  "
                      f"[{st['lo']:+.4f},{st['hi']:+.4f}]{dd:>19}{h2:>17}")
            print("    Pick the widest option that does not cost residual: a "
                  "retrace threshold")
            print("    tighter than the evidence supports is an unmeasured "
                  "gate wearing a number.")
    if args.crosscheck:
        wide = load(args.crosscheck, args.market)
        if wide:
            loose = dict(c)
            loose["retrace"] = (1.01, False)  # not stored there; ignored
            sel = [r for r in wide if admits(r, loose)]
            w = score(sel, draws=6000)
            d = delta(sel, wide)
            wb = score(wide, draws=6000)
            print(f"{NL}  CROSS-CHECK on {args.crosscheck}")
            print(f"    that corpus ungated: n={wb['n']} {wb['win']:.1%} "
                  f"{wb['mu']:+.4f}" if wb else "    unscoreable")
            if w:
                print(f"    same set, retrace ignored: n={w['n']} "
                      f"({len(sel)/len(wide):.1%})  {w['win']:.1%}  "
                      f"{w['mu']:+.4f} [{w['lo']:+.4f},{w['hi']:+.4f}]"
                      + ("  holds" if w["lo"] > 0 else "  spans zero"))
                if d:
                    print(f"    vs taking everything there: {d['mu']:+.4f} "
                          f"[{d['lo']:+.4f},{d['hi']:+.4f}]"
                          + ("  gating adds" if d["lo"] > 0 else "  not shown"))
            else:
                print("    too few matching points to score")
    print("\n  CONFIG KEYS")
    print(json.dumps({
        "min_ask": c["price"][0], "max_ask": c["price"][1],
        "min_abs_distance_bps": c["gap"][0] or None,
        "max_abs_distance_bps": c["gap"][1],
        "max_brti_momentum_bps": c["mom"],
        "min_brti_accel": -c["accel"], "max_brti_accel": c["accel"],
        "min_brti_held_s": c["held"], "min_brti_rejections": c["rej"],
        "entry_from_seconds": c["win"][0], "entry_to_seconds": c["win"][1],
        "max_brti_retrace": c["retrace"][0],
        "require_measurable_retrace": c["retrace"][1],
    }, indent=2))


if __name__ == "__main__":
    main()
