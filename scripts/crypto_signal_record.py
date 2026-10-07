"""Win-loss by lifecycle group, and what the intelligence decided, per crypto.

TWO QUESTIONS, KEPT APART, because a single percentage answers neither:

  THE CALL RECORD   for every market, did the side the reference implied win?
                    Counted for BLOCKED markets too - a refused signal is still
                    a prediction that was right or wrong, and scoring only the
                    subset the gates took measures the gates, not the strategy.

  THE INTELLIGENCE  what the learned layer actually DID, from
                    `intelligence_decisions` - not what it could do in
                    principle. `final_action` is its verdict, `overrides_gate`
                    says whether it changed the rule's answer, and the arms in
                    the policy file say whether it has the authority to.

Both are read from the running instances' databases READ-ONLY. Nothing here
writes, because these are live trading databases of running services - opening
one read-write runs the store's migrations against it.

No fees, per the operator's standing instruction.
"""

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal import market_lifecycle as lc  # noqa: E402

CRYPTO = [
    ("BTC", "btc15.db", "KXBTC15M", "runtime"),
    ("ETH", "eth15.db", "KXETH15M", "runtime-eth"),
    ("SOL", "sol15.db", "KXSOL15M", "runtime-sol"),
]


def wl(rows):
    """(W, L, win%) over graded markets."""
    w = sum(1 for x in rows if x.outcome == lc.WON)
    losses = sum(1 for x in rows if x.outcome == lc.LOST)
    total = w + losses
    return w, losses, (w / total if total else 0.0)


def line(label, rows):
    w, losses, rate = wl(rows)
    if w + losses == 0:
        print(f"    {label:<26}{'-':>18}")
        return
    print(f"    {label:<26}{f'{w}W-{losses}L':>10}{rate:>8.1%}")


def intelligence(db, runtime):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    total = con.execute("SELECT COUNT(*) FROM intelligence_decisions").fetchone()[0]
    actions = Counter()
    for (a,) in con.execute(
            "SELECT COALESCE(final_action,'(none)') FROM intelligence_decisions"):
        actions[a] += 1
    overrode = con.execute(
        "SELECT COUNT(*) FROM intelligence_decisions "
        " WHERE overrides_gate IS NOT NULL AND overrides_gate != ''"
    ).fetchone()[0]
    # Where it had an opinion that DIFFERED from the rule - the only rows where
    # it could have been right or wrong about anything.
    disagreed = con.execute(
        "SELECT COUNT(*) FROM intelligence_decisions "
        " WHERE base_qualified IS NOT NULL AND final_action IS NOT NULL "
        "   AND ((base_qualified = 1 AND final_action = 'veto') "
        "     OR (base_qualified = 0 AND final_action = 'admit'))"
    ).fetchone()[0]
    evid = con.execute(
        "SELECT COUNT(*), MAX(evidence_n) FROM intelligence_decisions "
        " WHERE evidence_n IS NOT NULL AND evidence_n > 0").fetchone()
    cand = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(would_change),0), "
        "       COALESCE(SUM(won),0) FROM candidate_evaluations").fetchone()
    con.close()

    pol = ROOT / runtime / "intelligence_policy.json"
    arms = promoted = 0
    vetoes = admissions = None
    version = "(no policy)"
    if pol.exists():
        o = json.loads(pol.read_text(encoding="utf-8"))
        version = o.get("version", "?")
        a = o.get("arms") or {}
        arms = len(a)
        promoted = sum(1 for v in a.values() if v.get("promoted"))
        vetoes = o.get("vetoes_enabled")
        admissions = o.get("admissions_enabled")
    return {"total": total, "actions": actions, "overrode": overrode,
            "disagreed": disagreed, "evid": evid, "cand": cand,
            "version": version, "arms": arms, "promoted": promoted,
            "vetoes": vetoes, "admissions": admissions}


def main():
    print("=" * 78)
    print("CRYPTO SIGNAL RECORD - win/loss by group, and the intelligence")
    print("=" * 78)
    for label, db, series, runtime in CRYPTO:
        if not (ROOT / db).exists():
            continue
        lives = list(lc.build(db).values())
        print(f"\n{'-' * 78}\n{label}  ({series})\n{'-' * 78}")

        graded = [x for x in lives if x.outcome in (lc.WON, lc.LOST)]
        print(f"  CALL RECORD - every market, refused ones included "
              f"({len(graded)} graded of {len(lives)} evaluated)")
        line("all calls", graded)
        line("blocked (never eligible)", [x for x in graded if not x.ever_eligible])
        line("let through", [x for x in graded if x.ever_eligible])
        line("  of which TRADED", [x for x in graded if x.traded])
        line("  eligible, not traded",
             [x for x in graded if x.ever_eligible and not x.traded])

        i = intelligence(db, runtime)
        print(f"\n  INTELLIGENCE - {i['total']} decisions recorded")
        print(f"    policy {i['version']}   arms {i['arms']}, "
              f"promoted {i['promoted']}")
        print(f"    vetoes_enabled {i['vetoes']}   "
              f"admissions_enabled {i['admissions']}")
        top = ", ".join(f"{k} {v}" for k, v in i["actions"].most_common(5))
        print(f"    actions: {top}")
        print(f"    changed the rule's answer: {i['overrode']}   "
              f"disagreed with it: {i['disagreed']}")
        n, mx = i["evid"]
        print(f"    decisions with cohort evidence: {n}, largest cohort "
              f"n={mx or 0}")
        cn, cchange, cwon = i["cand"]
        print(f"    shadow candidates graded: {cn}, would have changed "
              f"{cchange}, of those markets {cwon} won")

    print(f"\n{'=' * 78}")
    print("The call record counts CALLS, so a blocked market still scores - a")
    print("refused signal is a prediction that was right or wrong. Money is not")
    print("shown here: see all_instruments_report.py, which reads the broker.")


if __name__ == "__main__":
    main()
