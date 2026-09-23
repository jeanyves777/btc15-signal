"""The learning loop's state and the end-to-end trace, as operating evidence.

Two things, in one place, because they answer the same question from opposite
ends:

    STATE   what is running, what is loaded, what is scheduled, what is active
    TRACE   one real decision followed all the way through -
            signal -> baseline -> intelligence -> final -> execution ->
            settlement -> evaluation

The trace matters more than the state. A state report can look healthy while
every decision returns the same refusal - that is precisely the condition this
work was started to fix, and it went unnoticed for a day because nothing ever
followed a single decision from end to end.

This script READS. It never trains, never activates and never writes a policy;
the loop inside the service does all of that by itself. Running this changes
nothing, which is what makes it safe to run against the live database while the
bot is trading.

    python scripts/learning_report.py                  # state + recent traces
    python scripts/learning_report.py --window <ms>    # trace one market
    python scripts/learning_report.py --out FILE       # also write to FILE
"""

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import (  # noqa: E402
    feature_contract,
    learning,
    learning_data,
    revision,
)
from btc15_signal import intelligence_policy as intel  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.learning_runner import LearningRunner  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

LINE = "=" * 78
THIN = "-" * 78


def when(ms) -> str:
    if not ms:
        return "never"
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.UTC).strftime(
        "%Y-%m-%d %H:%M:%SZ"
    )


def state_section(runner: LearningRunner, now_ms: int, out: list) -> None:
    snap = runner.snapshot(now_ms)
    out.append(LINE)
    out.append("LEARNING LOOP - STATE")
    out.append(LINE)
    out.append("")
    out.append("  FOUR STATES, kept apart because they are four different claims:")
    for label, key, detail in (
        ("running", "running", f"every {snap['interval_ms'] // 3600000}h or "
                               f"{snap['trigger_threshold']} new settled markets"),
        ("updating", "updating", "a fit is in progress right now"),
        ("adjusting confidence", "adjusting_confidence",
         f"{snap['confidence_arms']} arm(s) carry a delta"),
        ("authorised to affect execution", "authorised_to_execute",
         f"{snap['promoted_arms']} promoted arm(s); mode {snap['mode']}"),
    ):
        mark = "YES" if snap[key] else "no "
        out.append(f"    {mark}  {label:<32} {detail}")
    out.append("")
    out.append(f"  mode                {snap['mode']}  ({snap['mode_reason']})")
    out.append(THIN)
    out.append("  LOADED POLICY")
    out.append(f"    version           {snap['policy_version']}")
    out.append(f"    valid             {snap['policy_valid']}"
               + ("" if snap["policy_valid"]
                  else f"   <-- {snap['policy_invalid_reason']}"))
    out.append(f"    feature version   {snap['feature_version']}")
    out.append(f"    feature contract  {snap['feature_fingerprint']}  "
               f"(live {feature_contract.FINGERPRINT})")
    out.append(f"    arms              {snap['arms']}")
    out.append(f"    training cutoff   {when(snap['training_cutoff_ms'])}")
    out.append(f"    data ends         {when(snap['data_end_ms'])}")
    out.append(f"    vetoes/admissions {snap['vetoes_enabled']} / "
               f"{snap['admissions_enabled']}")
    act = snap.get("activation") or {}
    if act:
        out.append(f"    activated         {when(act.get('activated_ms'))} "
                   f"({act.get('kind')})")
        out.append(f"    replaced          {act.get('previous_version')}")
        out.append(f"    because           {str(act.get('reason'))[:100]}")
    out.append(THIN)
    out.append("  TRAINING SCHEDULE")
    out.append(f"    last successful   {when(snap['last_success_ms'])}")
    out.append(f"    settled markets   {snap['settled_markets']} "
               f"({snap['new_settled_markets']} new since last training, "
               f"trigger at {snap['trigger_threshold']})")
    out.append(f"    next training     {when(snap['next_due_ms'])}")
    out.append(f"    run counts        {snap['run_counts']}")
    if snap["last_error"]:
        out.append(f"    LAST FAILURE      {when(snap['last_error_ms'])} "
                   f"({snap['consecutive_failures']} consecutive)")
        out.append(f"      {snap['last_error'][:200]}")
        out.append("      the last valid Kalshi policy remained active")
    last = snap.get("last_complete_run") or {}
    if last:
        out.append(THIN)
        out.append("  LAST COMPLETED RUN - WHAT DATA IT USED")
        out.append(f"    trigger           {last.get('trigger')}  "
                   f"status {last.get('status')}")
        out.append(f"    ran               {when(last.get('started_ms'))} -> "
                   f"{when(last.get('finished_ms'))}")
        out.append(f"    markets           {last.get('markets_used')} "
                   f"({last.get('rows_used')} decisions)")
        out.append(f"      archive         {last.get('corpus_markets')}")
        out.append(f"      live            {last.get('live_markets')} "
                   f"({last.get('live_actual_fills')} real fills, the rest "
                   f"simulated at the recorded ask)")
        out.append(f"    excluded          "
                   f"{last.get('excluded_unresolved')} unresolved, "
                   f"{last.get('excluded_duplicate')} duplicate polls, "
                   f"{last.get('excluded_incompatible')} incompatible")
        out.append(f"    fitted            {last.get('arms_fitted')} arms, "
                   f"{last.get('arms_with_confidence')} with confidence")
        out.append(f"    promoted          {last.get('promoted')} of "
                   f"{last.get('candidates_examined')} examined")
        out.append(f"    activated         {bool(last.get('activated'))}")
    out.append(THIN)
    out.append("  ACTIVE ADJUSTMENTS")
    adjustments = snap.get("active_adjustments") or []
    if not adjustments:
        out.append("    none - every decision returns the base strategy with "
                   "its evidence beside it")
    for item in adjustments:
        out.append(f"    [{item['kind']}] {item['context_key']}")
        out.append(f"        delta {item['delta']:+d}  action "
                   f"{item['action']}  promoted {item['promoted']}")
        out.append(f"        n={item['n']} over {item['markets']} markets, "
                   f"{item['mean']:+.4f}/ct [{item['low']:+.4f},"
                   f"{item['high']:+.4f}]")
        out.append(f"        {item['reason']}")
    for item in snap.get("withdrawals") or []:
        out.append(f"    WITHDRAWN {item.get('context_key')} - "
                   f"{item.get('reason')}")
    out.append("")


def trace_section(store: Store, runner: LearningRunner, window: int | None,
                  out: list, limit: int = 3) -> None:
    """Follow real decisions end to end. Nothing here is constructed."""
    db = store.db
    cursor = db.cursor()
    cursor.row_factory = sqlite3.Row
    if window:
        windows = [window]
    else:
        windows = [
            r[0] for r in db.execute(
                "SELECT DISTINCT window_open FROM intelligence_decisions "
                "WHERE graded_ms IS NOT NULL AND context_key LIKE '%bd%' "
                "ORDER BY window_open DESC LIMIT ?", (limit,)
            )
        ]
    out.append(LINE)
    out.append("TRACE - signal -> baseline -> intelligence -> final -> "
               "execution -> settlement -> evaluation")
    out.append(LINE)
    if not windows:
        out.append("")
        out.append("  no settled Kalshi-keyed decision yet.")
        out.append("")
        return
    policy = intel.Policy.load(runner.policy_path)
    for w in windows:
        rows = [dict(r) for r in cursor.execute(
            "SELECT * FROM intelligence_decisions WHERE window_open = ? "
            "ORDER BY decided_ms", (w,)
        )]
        if not rows:
            continue
        out.append("")
        out.append(f"  MARKET {when(w)}   window_open={w}   "
                   f"({len(rows)} polls = {len(rows)} decisions, 1 market)")
        first = rows[0]
        # The representative decision: the first poll that QUALIFIED, which is
        # where the bot alerts and stops. Otherwise the first poll looked at.
        chosen = next((r for r in rows if r.get("base_qualified")), first)
        out.append(f"    1 SIGNAL        side={chosen.get('side')} "
                   f"ask={chosen.get('ask')} "
                   f"remaining={chosen.get('remaining_s')}s "
                   f"at {when(chosen.get('decided_ms'))}")
        out.append(f"                    context {chosen.get('context_key')}")
        out.append(f"                    features {chosen.get('feature_version')}"
                   f"  (live contract {feature_contract.FINGERPRINT})")
        gates = chosen.get("failed_gates")
        out.append(f"    2 BASELINE      "
                   f"{'QUALIFIED' if chosen.get('base_qualified') else 'REFUSED'}"
                   + (f" - {gates}" if gates else " - all gates passed"))
        evidence = (chosen.get("evidence_action")
                    or chosen.get("final_action"))
        ev_delta = (chosen.get("evidence_delta")
                    or chosen.get("confidence_delta"))
        out.append(f"    3 INTELLIGENCE  evidence={evidence} "
                   f"delta={ev_delta} n={chosen.get('evidence_n')}")
        out.append(f"                    reason: {chosen.get('reason')}")
        if chosen.get("authority"):
            out.append(f"                    authority withheld: "
                       f"{chosen.get('authority')}")
        allowed = (chosen.get("base_qualified")
                   or chosen.get("final_action") == "admit")
        out.append(f"    4 FINAL         applied={chosen.get('final_action')} "
                   f"delta={chosen.get('confidence_delta')}  -> trade "
                   f"{'ALLOWED' if allowed else 'NOT TAKEN'}")
        # Execution: the broker's record, never a rebuild.
        ticker = chosen.get("ticker")
        fill_count = 0
        settlement = None
        if ticker:
            fill_count = cursor.execute(
                "SELECT COUNT(*) FROM fills WHERE ticker = ? "
                "AND action='buy'", (ticker,)
            ).fetchone()[0]
            settlement = cursor.execute(
                "SELECT * FROM settlements WHERE ticker = ? LIMIT 1", (ticker,)
            ).fetchone()
        position = learning_data._position_from(dict(settlement) if settlement
                                                else None)
        if position and position["side"] == chosen.get("side"):
            out.append(f"    5 EXECUTION     FILLED (broker) "
                       f"x{position['contracts']:g} contracts "
                       f"@ {position['price']:.4f} "
                       f"fee {position['fee_per_contract']:.4f}/ct "
                       f"({fill_count} fill(s))")
        elif position:
            out.append(f"    5 EXECUTION     a {position['side']} position "
                       f"settled here, not this row's "
                       f"{chosen.get('side')} - SIMULATED for this decision")
        else:
            out.append("    5 EXECUTION     no position recorded for this "
                       "market - anything scored here is a SIMULATED fill at "
                       "the recorded ask")
        if settlement:
            s = dict(settlement)
            out.append(f"    6 SETTLEMENT    result={s.get('market_result')} "
                       f"pnl={s.get('pnl')} fee={s.get('fee_cost')} "
                       f"settled {when(s.get('settled_ms'))}  [from Kalshi]")
        else:
            out.append(f"    6 SETTLEMENT    graded won={chosen.get('won')} "
                       f"at {when(chosen.get('graded_ms'))} "
                       f"(no broker settlement row - not a traded market)")
        evals = [dict(r) for r in cursor.execute(
            "SELECT * FROM candidate_evaluations WHERE window_open = ?", (w,)
        )]
        if evals:
            for e in evals:
                out.append(f"    7 EVALUATION    candidate {e['candidate_id']} "
                           f"({e['proposed_action']}) would_change="
                           f"{e['would_change']} won={e['won']}")
                out.append(f"                    baseline {e['baseline_pnl']:+.4f}"
                           f"  candidate {e['candidate_pnl']:+.4f}"
                           f"  incremental "
                           f"{(e['candidate_pnl'] or 0) - (e['baseline_pnl'] or 0):+.4f}")
        else:
            out.append("    7 EVALUATION    no candidate speaks to this "
                       "context (a table of non-opinions buries the opinions)")
        # And what the CURRENT policy says about this same cell now.
        key = chosen.get("context_key") or ""
        if key and policy.active:
            v = intel.decide(
                context_key=key,
                base_qualified=bool(chosen.get("base_qualified")),
                failed_gates=tuple(
                    g.strip() for g in (gates or "").split(",") if g.strip()
                ),
                ask=chosen.get("ask"), policy=policy, enabled=True,
                features_ok=True,
            )
            out.append(f"    * TODAY         the CURRENT policy "
                       f"({policy.version}) on this cell: "
                       f"{v.final_action} delta={v.confidence_delta} "
                       f"n={v.evidence_n}")
            out.append(f"                    {v.reason}")
    out.append("")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--traces", type=int, default=3)
    args = parser.parse_args()

    settings = Settings()
    store = Store(settings.database_path)
    runner = LearningRunner(settings, store)
    now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)

    out: list[str] = []
    out.append(f"generated {when(now_ms)}")
    rev = revision.REVISION
    out.append(f"running revision: {rev['short']} ({rev['branch']}, "
               f"{'DIRTY' if rev['dirty'] else 'clean'})  "
               f"source fingerprint {rev['fingerprint']}")
    out.append(f"  {rev['subject']}")
    out.append(f"feature contract: {feature_contract.describe()}")
    out.append(f"promotion bar: train n>={learning.MIN_PROMOTION_N}, "
               f"validate n>={learning.MIN_VALIDATE_N}, widened interval clear "
               f"of zero; confidence n>={learning.MIN_CONFIDENCE_N}")
    out.append("")
    state_section(runner, now_ms, out)
    trace_section(store, runner, args.window, out, args.traces)
    text = "\n".join(out)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
