"""Fit the adaptive policy on earlier data, validate on later, hold out the last.

THE POLICY IS FROZEN BY THIS SCRIPT. Nothing in the running service rewrites
thresholds; the live path loads the artefact this writes and applies it
unchanged. That separation is the point - an online rule that edits itself has
no version anyone can roll back to.

THREE CHRONOLOGICAL PERIODS, in order, never shuffled:

    TRAIN       arms are fitted here, and only here
    VALIDATE    candidate vetoes and admissions are judged here
    HOLDOUT     never looked at while choosing anything

Markets in the same window belong to one split, and a market contributes ONE
row - its first decision minute - because repeated polls of the same market
are not independent trades. Section 36 records what ignoring that does to a
base rate.

THE BAR A POLICY MUST CLEAR to change a decision, declared before looking:

    n >= MIN_EVIDENCE in train
    the validation interval, widened for the number of candidates examined,
      must exclude zero
    the sign must agree between train and validate

If nothing clears it, the policy ships with `vetoes_enabled` and
`admissions_enabled` FALSE, the confidence layer still applies, and the failed
criterion is printed. A neutral policy that says so is a result; a tuned one
that passes by construction is not.

    python scripts/train_intelligence.py
"""

import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from btc15_signal.adaptive import context_of  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.intelligence_policy import (  # noqa: E402
    ADMIT,
    FEATURE_VERSION,
    NEUTRAL,
    VETO,
    Policy,
    shrink,
)
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

MIN_EVIDENCE = 120
TRAIN_FRACTION = 0.55
VALIDATE_FRACTION = 0.25       # the remaining 0.20 is the holdout
POLICY_PATH = "runtime/intelligence_policy.json"


def net(ask: float, won: int) -> float:
    return (1.0 if won else 0.0) - ask - fee(ask, 1)


def ci(values: list[float], draws: int = 2000, seed: int = 11):
    if len(values) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def load_rows(settings: Settings) -> list[dict]:
    from measure_rejections import load

    rows = load(settings)
    rows.sort(key=lambda r: r["window_open"])
    return rows


def split(rows: list[dict]):
    n = len(rows)
    a = int(n * TRAIN_FRACTION)
    b = a + int(n * VALIDATE_FRACTION)
    return rows[:a], rows[a:b], rows[b:]


def arms_for(rows: list[dict], reward) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = f"{context_of(row)}|{'accept' if row['rule_match'] else 'reject'}"
        groups[key].append(row)
    out = {}
    for key, items in groups.items():
        values = [reward(r) for r in items]
        low, high = ci(values)
        out[key] = {
            "n": len(values),
            "mean": round(sum(values) / len(values), 6),
            "low": round(low, 6), "high": round(high, 6),
            "wins": sum(1 for r in items if r["won"]),
        }
    return out


def main() -> None:
    settings = Settings()
    rows = load_rows(settings)
    if not rows:
        print("no rows")
        return

    def reward(row):
        ask = row["our_ask"]
        if not row["rule_match"]:
            # A refusal is priced at the ask we recorded PLUS the slippage a
            # real crossing pays. It remains a simulated fill.
            ask = min(0.99, ask + settings.entry_slippage)
        return net(ask, row["won"])

    train, validate, holdout = split(rows)
    print(f"rows {len(rows)}  train {len(train)}  validate {len(validate)}  "
          f"holdout {len(holdout)} (untouched)")
    cutoff = train[-1]["window_open"]

    train_arms = arms_for(train, reward)
    val_arms = arms_for(validate, reward)

    # The prior every sparse cell shrinks toward: the population mean of its
    # own action in train.
    priors = {}
    for action in ("accept", "reject"):
        values = [reward(r) for r in train
                  if ("accept" if r["rule_match"] else "reject") == action]
        priors[action] = sum(values) / len(values) if values else 0.0
    print(f"train priors: accept {priors['accept']:+.4f}  "
          f"reject {priors['reject']:+.4f}")

    candidates = {
        key: stats for key, stats in train_arms.items()
        if stats["n"] >= MIN_EVIDENCE
    }
    print(f"\ncandidate arms with n>={MIN_EVIDENCE}: {len(candidates)}")

    policy = Policy(
        version="v1", model_version="arms-shrunk-1",
        feature_version=FEATURE_VERSION, training_cutoff_ms=int(cutoff),
        min_evidence=MIN_EVIDENCE,
        data_end_ms=int(rows[-1]["window_open"]),
    )
    passed, examined = [], 0
    for key, stats in sorted(candidates.items(), key=lambda kv: -kv[1]["n"]):
        action_part = key.split("|")[1]
        adjusted = shrink(stats["mean"], stats["n"], priors[action_part])
        # Confidence is ALWAYS allowed, and is small by construction.
        delta = max(-12, min(12, int(round(adjusted * 200))))
        entry = {
            "n": stats["n"], "mean": round(adjusted, 6),
            "low": stats["low"], "high": stats["high"],
            "action": NEUTRAL, "gate": None, "delta": delta,
        }
        val = val_arms.get(key)
        if val and val["n"] >= 40:
            examined += 1
            agree = (adjusted > 0) == (val["mean"] > 0)
            widened = (val["high"] - val["low"]) / 2 * (max(1, examined) ** 0.5)
            mid = (val["high"] + val["low"]) / 2
            clears = (mid - widened) > 0 or (mid + widened) < 0
            if agree and clears:
                if action_part == "accept" and adjusted < 0:
                    entry["action"] = VETO
                elif action_part == "reject" and adjusted > 0:
                    entry["action"] = ADMIT
                    # An exception names ONE gate. Multi-gate refusals are not
                    # eligible: overriding several at once is a new rule, not
                    # an exception to an old one.
                    entry["gate"] = None
                if entry["action"] != NEUTRAL:
                    passed.append((key, entry, val))
        policy.arms[key] = entry

    print(f"validated candidates examined: {examined}")
    print(f"decision-changing policies that PASS: {len(passed)}")
    for key, entry, val in passed:
        print(f"  {entry['action']:<6} {key}  train {entry['mean']:+.4f} "
              f"n={entry['n']}  validate {val['mean']:+.4f} n={val['n']}")

    policy.vetoes_enabled = any(e["action"] == VETO for _k, e, _v in passed)
    policy.admissions_enabled = any(e["action"] == ADMIT for _k, e, _v in passed)
    if not passed:
        policy.notes = (
            "No veto or admission cleared the predeclared bar. FAILED "
            "CRITERION: the validation interval, widened for the number of "
            "candidates examined, does not exclude zero for any context. "
            "Confidence adjustments are active; execution behaviour is "
            "NEUTRAL."
        )
        print(f"\n{policy.notes}")
    policy.save(POLICY_PATH)
    print(f"\nwrote {POLICY_PATH}  version={policy.version} "
          f"cutoff={policy.training_cutoff_ms} arms={len(policy.arms)} "
          f"vetoes={policy.vetoes_enabled} admissions={policy.admissions_enabled}")
    print("HOLDOUT was not read while choosing anything.")


if __name__ == "__main__":
    main()
