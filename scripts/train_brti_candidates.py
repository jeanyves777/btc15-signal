"""Freeze versioned CANDIDATES on BRTI features, for prospective evaluation.

Two things the previous pass got wrong, both of which the operator named.

FIRST, the features were Binance-derived - the instrument that disagrees with
the official settlement on 19.4% of outcomes. Every context was potentially
mislabelled before an interval was computed. This trains on
`brti_decision_points`: the distance, momentum and volatility the live path
now uses, against the price the contract settles on.

SECOND, and more important: a candidate does NOT have to pass the promotion
bar before it can be forward-tested. Those are different questions.

    promotion   may this change a live order?      needs the full bar
    candidate   is this worth watching forwards?   needs only evidence

A candidate is frozen here with a version, recorded against every subsequent
live signal alongside what the unchanged strategy decided, and graded when the
market settles. It controls nothing. "No adjustment has earned promotion" is a
result; "therefore there is nothing to forward-test" does not follow from it,
and treating it as if it did is how a learning loop never starts.

    python scripts/train_brti_candidates.py
"""

import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from brti_dataset import brti_context, load_policy_rows  # noqa: E402

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.intelligence_policy import ADMIT, NEUTRAL, VETO, shrink  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

CANDIDATES_PATH = "runtime/intelligence_candidates.json"
MIN_CANDIDATE_N = 60      # enough to be worth WATCHING
MIN_PROMOTION_N = 120     # enough to be considered for CONTROLLING an order
TRAIN_FRACTION = 0.55
VALIDATE_FRACTION = 0.25


def net(ask: float, won: int) -> float:
    return (1.0 if won else 0.0) - ask - fee(ask, 1)


def cluster_ci(values: list[float], groups: list, draws: int = 2000, seed: int = 11):
    """Bootstrap over DAYS, not rows.

    Markets in one session move together - a trend that carries five windows
    carries their outcomes with it - so resampling rows treats correlated
    observations as independent and reports an interval far too narrow. The
    unit resampled here is the day.
    """
    if len(values) < 2:
        return 0.0, 0.0
    buckets = defaultdict(list)
    for value, group in zip(values, groups, strict=True):
        buckets[group].append(value)
    keys = list(buckets)
    if len(keys) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        pool = []
        for _ in keys:
            pool.extend(buckets[rng.choice(keys)])
        means.append(sum(pool) / len(pool))
    means.sort()
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def day_of(ms: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d")


def arms_for(rows, reward):
    groups = defaultdict(list)
    for row in rows:
        key = f"{brti_context(row)}|{'accept' if row['rule_match'] else 'reject'}"
        groups[key].append(row)
    out = {}
    for key, items in groups.items():
        values = [reward(r) for r in items]
        days = [day_of(r["window_open"]) for r in items]
        low, high = cluster_ci(values, days)
        out[key] = {
            "n": len(values), "days": len(set(days)),
            "mean": round(sum(values) / len(values), 6),
            "low": round(low, 6), "high": round(high, 6),
        }
    return out


def main() -> None:
    settings = Settings()
    rows = load_policy_rows()
    if not rows:
        print("no BRTI rows - run scripts/backfill_brti.py first")
        return

    def reward(row):
        ask = row["our_ask"]
        if not row["rule_match"]:
            ask = min(0.99, ask + settings.entry_slippage)
        return net(ask, row["won"])

    n = len(rows)
    a, b = int(n * TRAIN_FRACTION), int(n * (TRAIN_FRACTION + VALIDATE_FRACTION))
    train, validate, holdout = rows[:a], rows[a:b], rows[b:]

    # Print the split AND its composition. The baseline below is a training
    # figure, so quoting it beside the corpus total invites the reading that
    # 679 + 2,856 should come to 6,428. Chronological, never shuffled:
    # markets in one session move together, so a random split leaks the
    # afternoon into the morning.
    print(f"{'split':<24}{'markets':>9}{'qualified':>11}{'rejected':>10}")
    for name, part in (("train", train), ("validate", validate),
                       ("holdout (untouched)", holdout)):
        q = sum(r["rule_match"] for r in part)
        print(f"{name:<24}{len(part):>9}{q:>11}{len(part) - q:>10}")
    total_q = sum(r["rule_match"] for r in rows)
    print(f"{'TOTAL':<24}{n:>9}{total_q:>11}{n - total_q:>10}")

    taken = [reward(r) for r in train if r["rule_match"]]
    refused = [reward(r) for r in train if not r["rule_match"]]
    print(f"\nBRTI BASELINE (TRAIN SPLIT ONLY): rule took "
          f"{sum(taken) / len(taken):+.4f}/ct over {len(taken)}; refused "
          f"{sum(refused) / len(refused):+.4f}/ct over {len(refused)}")

    train_arms = arms_for(train, reward)
    val_arms = arms_for(validate, reward)
    priors = {
        "accept": sum(taken) / len(taken) if taken else 0.0,
        "reject": sum(refused) / len(refused) if refused else 0.0,
    }

    candidates, examined = [], 0
    for key, stats in sorted(train_arms.items(), key=lambda kv: -kv[1]["n"]):
        if stats["n"] < MIN_CANDIDATE_N:
            continue
        action_part = key.split("|")[1]
        adjusted = shrink(stats["mean"], stats["n"], priors[action_part])
        proposed = NEUTRAL
        if action_part == "accept" and adjusted < 0:
            proposed = VETO
        elif action_part == "reject" and adjusted > 0:
            proposed = ADMIT
        if proposed == NEUTRAL:
            continue
        val = val_arms.get(key, {})
        examined += 1
        promotes = False
        if val.get("n", 0) >= 40:
            agree = (adjusted > 0) == (val["mean"] > 0)
            widened = (val["high"] - val["low"]) / 2 * (max(1, examined) ** 0.5)
            mid = (val["high"] + val["low"]) / 2
            promotes = (
                agree and stats["n"] >= MIN_PROMOTION_N
                and ((mid - widened) > 0 or (mid + widened) < 0)
            )
        candidates.append({
            "candidate_id": f"c{len(candidates) + 1:02d}",
            "context": key, "proposed_action": proposed,
            "train_n": stats["n"], "train_days": stats["days"],
            "train_mean": round(adjusted, 6),
            "train_low": stats["low"], "train_high": stats["high"],
            "validate_n": val.get("n", 0),
            "validate_mean": val.get("mean"),
            "promotes": promotes,
            "delta": max(-12, min(12, int(round(adjusted * 200)))),
        })

    passed = [c for c in candidates if c["promotes"]]
    print(f"\ncandidates frozen for FORWARD evaluation: {len(candidates)}")
    for c in candidates[:10]:
        print(f"  {c['candidate_id']} {c['proposed_action']:<6} {c['context']}")
        print(f"      train n={c['train_n']} over {c['train_days']} days "
              f"{c['train_mean']:+.4f} [{c['train_low']:+.4f},{c['train_high']:+.4f}]"
              f"  validate n={c['validate_n']}")
        if c["validate_mean"] is not None:
            print(f"      validate mean {c['validate_mean']:+.4f}")
    print(f"\ncandidates that would PROMOTE today: {len(passed)}")
    if not passed:
        print("  none. They are still forward-tested; promotion is a separate bar.")

    artefact = {
        "version": f"brti-cand-{int(time.time())}",
        "feature_version": "brti-1",
        "built_ms": int(time.time() * 1000),
        "data_end_ms": int(rows[-1]["window_open"]),
        "training_cutoff_ms": int(train[-1]["window_open"]),
        "min_candidate_n": MIN_CANDIDATE_N,
        "min_promotion_n": MIN_PROMOTION_N,
        "candidates": candidates,
    }
    Path(CANDIDATES_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(CANDIDATES_PATH).write_text(json.dumps(artefact, indent=2, sort_keys=True))
    print(f"\nwrote {CANDIDATES_PATH}  version={artefact['version']}")
    print("HOLDOUT untouched. Candidates control nothing; they are recorded "
          "against live signals and graded at settlement.")


if __name__ == "__main__":
    main()
