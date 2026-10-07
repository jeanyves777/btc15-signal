"""The combo recovery CHAIN, tested forward on resampled real outcomes.

THE RULE, operator 2026-09-27. Per original loss L0, at most three combos, each
with the same $2.00 budget (nothing escalates), each fired under today's rules -
armed, 5-window life, trigger leg in 0.70-0.79 - with the operator's pairing:
BTC's partner is SOL, ETH's is SOL, SOL's is BTC.

    combo 1   must clear L0 if right                     win -> stop, recovered
    combo 2   only if combo 1 lost (L1): must clear at least 50% of L1
    combo 3   at the next opportunity after combo 2: must clear L0, then STOP

WHY THIS IS SIMULATED RATHER THAN REPLAYED. The real record contains one
recovery combo and it won, so steps 2 and 3 never trigger on it; the operator
noted that two losses in a row have not been seen since the system improved,
and asked for the chain to be tested in preparation. A replay of one event
cannot say anything about a chain. So the chain is run forward, drawing every
combo from REAL combo opportunities:

    an opportunity = a window in which the trigger instrument was rule-qualified
    with its ask inside 0.70-0.79 AND its allowed partner was rule-qualified at
    that same instant (no later - a lookahead is refused). Its price is the
    product of the two recorded asks, and it won iff both legs actually settled
    their way.

TODAY'S RECOVERY IS RUN THE SAME WAY, from the SAME opportunities: a 2-contract
single-leg trade on the trigger instrument at its recorded ask, won iff that leg
settled its way. It continues the way the deployed rule does - a losing recovery
is a new loss and re-arms - to the same maximum of three trades. Because both
policies draw the SAME opportunity for each step, the comparison is paired.

L0 is drawn from the real 1-contract losses in the bot's trade record.

UNCERTAINTY IS DAY-CLUSTERED. Outcomes within a day move together, so the
opportunity pool is resampled by DAY for every outer draw, and the chains are
run inside each resample. The interval on the mean is the spread across those
outer draws, not the spread of individual chains.

AVAILABILITY IS REPORTED, NOT ASSUMED AWAY. The chain can only run when a
partner is qualified at the same instant. The share of recovery opportunities
that had one is printed first, because it decides how often any of this happens.

No fees, per the operator's standing instruction. Held to settlement.

    python scripts/recovery_chain_test.py
"""

import argparse
import bisect
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import recovery_combo_replay as R  # noqa: E402  - same stores, same loaders

BAND = R.BAND
BUDGET = R.BUDGET
CAP = R.CAP
STALE_S = R.STALE_S
ALLOWED = {"BTC": ("SOL",), "ETH": ("SOL",), "SOL": ("BTC",)}
SECOND_STEP_SHARE = 0.50     # combo 2 must clear at least half of combo 1's loss
MAX_STEPS = 3


def day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


def opportunities(book):
    """Every real recovery opportunity: first in-band qualified moment/window.

    Returns (single, combos): `single` holds every in-band qualified trigger
    moment - today's recovery could take any of them; `combos` the subset where
    the allowed partner was qualified at that same instant.
    """
    single, combos = [], []
    for asset, partners in ALLOWED.items():
        for window, (times, vals) in book.get(asset, {}).items():
            for t, (qualified, side, ask, won) in zip(times, vals):
                if not qualified or won is None:
                    continue
                if not BAND[0] <= ask <= BAND[1]:
                    continue
                opp = {"asset": asset, "window": window, "t": t,
                       "ask": ask, "won": won, "day": day(window)}
                single.append(opp)
                for other in partners:
                    p = R.partner_at(book, other, window, t)
                    if p is None:
                        continue
                    assert p["decided_ms"] <= t, "partner from the future"
                    combos.append(dict(
                        opp, partner=other, p_ask=p["ask"], p_won=p["won"],
                        price=ask * p["ask"], combo_won=won and p["won"]))
                    break
                break  # first in-band qualified moment per window only
    return single, combos


def real_losses():
    """1-contract losses from the bot's real trades: the L0 a chain starts from."""
    out = []
    for asset in ALLOWED:
        for e in R.load_entries(asset):
            if not e["won"]:
                out.append(e["ask"])
    return out


def combo_size(price):
    return min(R.contracts_for_budget(BUDGET, price), CAP)


def eligible(pool, target):
    """Combos from `pool` whose profit if right clears `target`."""
    return [c for c in pool
            if c["price"] < 1.0
            and combo_size(c["price"]) * (1.0 - c["price"]) >= target]


def run_chain(rng, l0, pool):
    """One original loss through the combo chain. Returns (net, steps, path)."""
    net, path, steps = -l0, [], 0
    # step 1: clear L0
    cands = eligible(pool, l0)
    if not cands:
        return net, steps, "no eligible combo"
    c = rng.choice(cands)
    n = combo_size(c["price"])
    steps += 1
    if c["combo_won"]:
        net += n * (1.0 - c["price"])
        return net, steps, "W"
    l1 = n * c["price"]
    net -= l1
    path.append("L")
    # step 2: clear at least half of combo 1's loss
    cands = eligible(pool, SECOND_STEP_SHARE * l1)
    if cands:
        c = rng.choice(cands)
        n = combo_size(c["price"])
        steps += 1
        if c["combo_won"]:
            net += n * (1.0 - c["price"])
            path.append("W")
        else:
            net -= n * c["price"]
            path.append("L")
    # step 3: clear L0 again, then stop whatever happens
    cands = eligible(pool, l0)
    if cands:
        c = rng.choice(cands)
        n = combo_size(c["price"])
        steps += 1
        if c["combo_won"]:
            net += n * (1.0 - c["price"])
            path.append("W")
        else:
            net -= n * c["price"]
            path.append("L")
    return net, steps, "".join(path)


def run_single(rng, l0, pool):
    """Today's recovery on the same kind of opportunity, as the rule behaves:
    2 contracts at the in-band ask; a losing recovery is a new loss and
    re-arms; at most three recovery trades, matching the chain's depth."""
    net, steps, path = -l0, 0, ""
    for _ in range(MAX_STEPS):
        o = rng.choice(pool)
        n = min(R.contracts_for_budget(BUDGET, o["ask"]), CAP)
        steps += 1
        if o["won"]:
            net += n * (1.0 - o["ask"])
            path += "W"
            break
        net -= n * o["ask"]
        path += "L"
    return net, steps, path


def summarise(nets):
    s = sorted(nets)
    return {
        "mean": statistics.fmean(s),
        "full": sum(1 for x in s if x >= 0) / len(s),
        "p5": s[int(0.05 * len(s))],
        "worst": s[0],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outer", type=int, default=400,
                    help="day-clustered resamples of the opportunity pool")
    ap.add_argument("--inner", type=int, default=2000,
                    help="chains simulated inside each resample")
    ap.add_argument("--seed", type=int, default=17)
    args = ap.parse_args()

    book, _ = R.load_partner_book()
    single, combos = opportunities(book)
    losses = real_losses()

    print("=" * 84)
    print("RECOVERY CHAIN - combo x3 vs today's single-leg, on real opportunities")
    print("=" * 84)
    by_asset = defaultdict(lambda: [0, 0])
    for o in single:
        by_asset[o["asset"]][0] += 1
    for c in combos:
        by_asset[c["asset"]][1] += 1
    print("  AVAILABILITY - in-band qualified recovery moments, and how many had "
          "the partner qualified at that instant")
    for a in ALLOWED:
        n, k = by_asset[a]
        print(f"    {a} (partner {'/'.join(ALLOWED[a])})  {n:>4} moments   "
              f"{k:>3} with a partner  ({k / max(n, 1):.0%})")
    tot_n = len(single)
    tot_k = len(combos)
    print(f"    all            {tot_n:>4} moments   {tot_k:>3} with a partner  "
          f"({tot_k / max(tot_n, 1):.0%})")
    if not combos:
        print("  no combo opportunities at all - nothing to test")
        return

    cw = sum(1 for c in combos if c["combo_won"])
    cp = statistics.fmean(c["price"] for c in combos)
    sw = sum(1 for o in single if o["won"])
    sp = statistics.fmean(o["ask"] for o in single)
    days = sorted({c["day"] for c in combos})
    print(f"\n  THE RAW MATERIAL")
    print(f"    combos      n={len(combos):>4}  won {cw:>3}  "
          f"win {cw / len(combos):6.1%}  mean price {cp:.3f}  "
          f"residual {cw / len(combos) - cp:+.4f}   over {len(days)} days")
    print(f"    single-leg  n={len(single):>4}  won {sw:>3}  "
          f"win {sw / len(single):6.1%}  mean ask   {sp:.3f}  "
          f"residual {sw / len(single) - sp:+.4f}")
    print(f"    original losses L0: n={len(losses)}  "
          f"mean {statistics.fmean(losses):.2f}  "
          f"range {min(losses):.2f}-{max(losses):.2f}")

    # ---- day-clustered outer bootstrap, paired inner simulation
    rng = random.Random(args.seed)
    combo_days = defaultdict(list)
    for c in combos:
        combo_days[c["day"]].append(c)
    single_days = defaultdict(list)
    for o in single:
        single_days[o["day"]].append(o)
    all_days = sorted(set(combo_days) | set(single_days))
    means_b, means_a, diffs, full_b, full_a = [], [], [], [], []
    pooled_b, pooled_a, paths = [], [], defaultdict(int)
    for _ in range(args.outer):
        pick = [rng.choice(all_days) for _ in all_days]
        cpool = [c for d in pick for c in combo_days.get(d, [])]
        spool = [o for d in pick for o in single_days.get(d, [])]
        if not cpool or not spool:
            continue
        nb, na = [], []
        for _ in range(args.inner):
            l0 = rng.choice(losses)
            b, _, path = run_chain(rng, l0, cpool)
            a, _, _ = run_single(rng, l0, spool)
            nb.append(b)
            na.append(a)
            paths[path] += 1
        means_b.append(statistics.fmean(nb))
        means_a.append(statistics.fmean(na))
        diffs.append(means_b[-1] - means_a[-1])
        full_b.append(sum(1 for x in nb if x >= 0) / len(nb))
        full_a.append(sum(1 for x in na if x >= 0) / len(na))
        pooled_b += nb
        pooled_a += na

    def ci(xs):
        s = sorted(xs)
        return s[int(0.025 * len(s))], s[int(0.975 * len(s))]

    sb, sa = summarise(pooled_b), summarise(pooled_a)
    print(f"\n  PER ORIGINAL LOSS - net after the recovery sequence "
          f"(starts at -L0; >= 0 means fully recovered)")
    print(f"    {'':<22}{'mean':>9}{'95% CI':>20}{'fully recovered':>18}"
          f"{'5th pct':>10}{'worst':>9}")
    lo, hi = ci(means_b)
    flo, fhi = ci(full_b)
    print(f"    {'combo chain (x3)':<22}{sb['mean']:>+9.3f}  [{lo:+.3f},{hi:+.3f}]"
          f"   {sb['full']:6.1%} [{flo:.0%}-{fhi:.0%}]{sb['p5']:>+10.2f}"
          f"{sb['worst']:>+9.2f}")
    lo, hi = ci(means_a)
    flo, fhi = ci(full_a)
    print(f"    {'single-leg (today)':<22}{sa['mean']:>+9.3f}  [{lo:+.3f},{hi:+.3f}]"
          f"   {sa['full']:6.1%} [{flo:.0%}-{fhi:.0%}]{sa['p5']:>+10.2f}"
          f"{sa['worst']:>+9.2f}")
    lo, hi = ci(diffs)
    print(f"    {'chain - single':<22}{statistics.fmean(diffs):>+9.3f}  "
          f"[{lo:+.3f},{hi:+.3f}]")

    total = sum(paths.values())
    print(f"\n  HOW THE CHAIN ENDED (share of simulated original losses)")
    names = {"W": "combo 1 won - recovered", "LW": "1 lost, 2 won, 3 not eligible",
             "LL": "1 lost, 2 lost, 3 not eligible", "LWW": "1 lost, 2 won, 3 won",
             "LWL": "1 lost, 2 won, 3 lost", "LLW": "1 lost, 2 lost, 3 won",
             "LLL": "all three lost", "L": "1 lost, nothing else eligible",
             "no eligible combo": "no combo cleared the check"}
    for k, v in sorted(paths.items(), key=lambda kv: -kv[1]):
        print(f"    {names.get(k, k):<34}{v / total:7.2%}")

    # ---- the arithmetic of the worst path, with the real average numbers
    n1 = combo_size(cp)
    l0 = statistics.fmean(losses)
    print(f"\n  WORST CASE, exact: all three combos lose. At the average combo "
          f"price {cp:.3f}, $2 buys {n1},")
    print(f"  so each loss costs {n1 * cp:.2f}: total {l0 + 3 * n1 * cp:.2f} "
          f"including L0 {l0:.2f}. Bounded, because the budget never "
          f"escalates;")
    print(f"  today's single-leg worst case over three recoveries is "
          f"{l0 + 3 * 2 * sp:.2f}.")


if __name__ == "__main__":
    main()
