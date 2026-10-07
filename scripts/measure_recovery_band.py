"""What does ONE EXTRA CONTRACT earn, by ask band? The recovery-band question.

THE OPERATOR'S INSTRUCTION, 2026-09-25: the post-loss upsize should not fire on
whatever trade comes next. It should WAIT for a better-paying setup - around a
0.70-0.79 ask - because that is where a fixed stake can actually dent a deficit.
"So that we are not making 20 cent profit on a recovery trade where normal sizing
can offer the same on a better opportunity."

The arithmetic behind that is not in dispute: the extra contract wins `1 - ask`
and loses `ask`, so at 0.90 it risks 90c to make 10c and at 0.75 it risks 75c to
make 25c. What IS in question is whether the WIN RATE rises fast enough with
price to offset it. A 0.90 contract that wins 93% of the time may beat a 0.75 one
that wins 74%. So the decision needs the expectation, not the payoff:

    EV per extra contract = p * (1 - ask) - (1 - p) * ask
                          = p - ask

which is the calibration residual itself - the same quantity every gate in this
system is fitted on. That identity is worth stating plainly: "where does an extra
contract pay most" and "where is the market most mispriced" are the SAME
question, and the answer does not depend on the payoff ratio at all.

So this reports, per ask band: the win rate, the mean ask, the residual (= EV per
contract per dollar of face), and the EV per DOLLAR STAKED, which is residual/ask
and is what a fixed dollar budget actually earns. The two rank bands differently
and the difference is the operator's point.

Day-clustered bootstrap. No fees, per the operator's standing instruction - noted
because the deployed `recovery_size` eligibility check uses NET profit, so its
thresholds are stricter than anything here.

    python scripts/measure_recovery_band.py --asset btc \
        --brti data/brti_history_v4.db --market data/market_data.db
"""

import argparse
import json
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve()
                                .parents[1] / "src"))

BANDS = [
    (0.60, 0.70, "0.60-0.70"),
    (0.70, 0.75, "0.70-0.75"),
    (0.75, 0.80, "0.75-0.80"),
    (0.80, 0.85, "0.80-0.85"),
    (0.85, 0.90, "0.85-0.90"),
    (0.90, 0.94, "0.90-0.93"),
]


def gate_filter(config: str):
    """The deployed rule, as a predicate over stored decision points.

    WHY THIS MATTERS MORE THAN THE UNGATED TABLE. The loss step only ever sizes
    a trade that has ALREADY passed every gate - it never causes one. So the
    population that decides where an extra contract pays is the QUALIFYING
    subset, not every decision point. The gates select on distance, momentum and
    level behaviour, all of which correlate with price, so the band ordering can
    differ completely between the two populations.
    """
    from btc15_signal.kalshi_brti import KalshiBRTIRule
    cfg = {k: v for k, v in json.loads(
        pathlib.Path(config).read_text(encoding="utf-8")).items()
        if not k.startswith("_")}
    cfg.pop("enabled", None)
    rule = KalshiBRTIRule(**cfg)

    class F:
        stale = False

        def __init__(self, r):
            self.value = r["brti_value"]
            self.target = r["target"]
            self.side = r["brti_side"]
            self.samples = r["samples"]
            self.signed_distance_bps = r["signed_distance_bps"]
            self.brti_momentum_bps = r["brti_momentum_bps"]
            self.brti_volatility_bps = r["brti_volatility_bps"]
            self.brti_normalized_distance = r["brti_normalized_distance"]
            self.brti_accel = r["brti_accel"]
            self.brti_held_s = r["brti_held_s"]
            self.brti_rejections = r["brti_rejections"]
            self.brti_retrace = (r["brti_retrace"]
                                 if "brti_retrace" in r.keys() else None)
            self.brti_choppiness = (r["brti_choppiness"]
                                    if "brti_choppiness" in r.keys() else None)

    def ok(row, ask, remaining):
        if not (rule.entry_to_seconds <= remaining <= rule.entry_from_seconds):
            return False
        facts = rule.check_facts(F(row), ask, remaining)
        return all(f["passed"] for f in facts if f.get("enabled", True))

    return ok, rule


import pathlib  # noqa: E402  - used by gate_filter


def load(brti: str, market: str, gate=None):
    m = sqlite3.connect(f"file:{market}?mode=ro", uri=True)
    book = {}
    for t, ts, bid, ask in m.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
        if bid is None or ask is None:
            continue
        ms = ts * 1000 if ts < 1e11 else ts
        book[(t, ms // 60000 * 60000)] = (bid, ask)
    m.close()
    b = sqlite3.connect(f"file:{brti}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    rows = []
    for r in b.execute(
            "SELECT * FROM brti_decision_points "
            " WHERE result IN ('yes','no') AND brti_side IS NOT NULL"):
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
        if gate is not None and not gate(r, ask, r["remaining_s"]):
            continue
        rows.append({
            "won": (r["result"] == "yes") == (r["brti_side"] == "UP"),
            "ask": ask, "rem": r["remaining_s"],
            "day": datetime.fromtimestamp(
                r["close_ms"] / 1000, timezone.utc).date(),
        })
    b.close()
    return rows


def boot(vals_by_day, draws=6000, seed=11):
    days = list(vals_by_day)
    if len(days) < 6:
        return None
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        vals = [x for d in pick for x in vals_by_day[d]]
        out.append(sum(vals) / len(vals))
    out.sort()
    return out[int(draws * 0.025)], out[int(draws * 0.975) - 1]


def report(rows, label):
    if len(rows) < 60:
        return None
    win = sum(1 for r in rows if r["won"]) / len(rows)
    ask = statistics.mean(r["ask"] for r in rows)
    per_contract = defaultdict(list)
    per_dollar = defaultdict(list)
    for r in rows:
        edge = (1.0 if r["won"] else 0.0) - r["ask"]
        per_contract[r["day"]].append(edge)
        # What a FIXED DOLLAR BUDGET earns: the same edge, divided by what one
        # contract costs. This is the ranking the operator is reasoning about.
        per_dollar[r["day"]].append(edge / r["ask"])
    mu_c = statistics.mean(x for v in per_contract.values() for x in v)
    mu_d = statistics.mean(x for v in per_dollar.values() for x in v)
    ci_c = boot(per_contract)
    ci_d = boot(per_dollar)
    days = len(per_contract)
    print(f"  {label:<12}{len(rows):>6}{days:>6}{win:>8.1%}{ask:>8.3f}"
          f"{mu_c:>+10.4f}"
          + (f"  [{ci_c[0]:+.4f},{ci_c[1]:+.4f}]" if ci_c else "  (few days)")
          + f"{mu_d:>+10.4f}"
          + (f"  [{ci_d[0]:+.4f},{ci_d[1]:+.4f}]" if ci_d else ""))
    return {"n": len(rows), "win": win, "ask": ask, "per_contract": mu_c,
            "per_dollar": mu_d, "ci_c": ci_c}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--asset", required=True)
    p.add_argument("--brti", required=True)
    p.add_argument("--market", required=True)
    p.add_argument("--band-lo", type=float, default=0.70,
                   help="operator's proposed recovery band, lower edge")
    p.add_argument("--band-hi", type=float, default=0.79)
    p.add_argument("--config", default=None,
                   help="apply this deployed rule, so only QUALIFYING setups "
                        "are measured - the population the loss step sizes")
    args = p.parse_args()

    gate = None
    if args.config:
        gate, rule = gate_filter(args.config)
    rows = load(args.brti, args.market, gate)
    if not rows:
        raise SystemExit("no priced rows")
    print("=" * 108)
    scope = ("QUALIFYING setups only, per " + args.config if args.config
             else "every decision point, UNGATED")
    print(f"{args.asset.upper()}  one extra contract, by ask band   "
          f"{len(rows)} points, {len({r['day'] for r in rows})} days")
    print(f"  scope: {scope}")
    print("=" * 108)
    print("  EV per contract == p - ask == the calibration residual. EV per "
          "dollar staked == residual / ask.")
    print(f"  {'band':<12}{'n':>6}{'days':>6}{'win%':>8}{'ask':>8}"
          f"{'per ct':>10}{'95% CI':>21}{'per $':>10}{'95% CI':>21}")
    for lo, hi, name in BANDS:
        report([r for r in rows if lo <= r["ask"] < hi], name)

    print()
    band = [r for r in rows if args.band_lo <= r["ask"] <= args.band_hi]
    rest = [r for r in rows if not (args.band_lo <= r["ask"] <= args.band_hi)]
    print(f"  THE OPERATOR'S BAND {args.band_lo}-{args.band_hi} against "
          f"everything else")
    print(f"  {'group':<12}{'n':>6}{'days':>6}{'win%':>8}{'ask':>8}"
          f"{'per ct':>10}{'95% CI':>21}{'per $':>10}{'95% CI':>21}")
    a = report(band, "in band")
    b = report(rest, "outside")
    if a and b:
        print(f"\n  per contract : {a['per_contract']:+.4f} in band against "
              f"{b['per_contract']:+.4f} outside")
        print(f"  per dollar   : {a['per_dollar']:+.4f} in band against "
              f"{b['per_dollar']:+.4f} outside")
        print(f"  share of all setups in band: {len(band) / len(rows):.1%} - "
              f"this is how long a waiting upsize would wait.")


if __name__ == "__main__":
    main()
