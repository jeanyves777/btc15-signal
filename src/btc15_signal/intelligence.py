"""The validation runner: gross-edge verdict, lifecycle study, regime findings.

Answers three questions in order, and stops caring about the later ones if the
first fails:

1. Does the strategy have a gross edge over the price it pays, out of sample?
2. If a trade lost, was it ever winning - could an early exit have saved it?
3. Does the edge live in a particular regime rather than everywhere?

Every answer, and the evidence behind it, is written to the intelligence ledger
so the next run starts from what this one learned.
"""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .datasource import DataCache
from .features import build_snapshots
from .gridsearch import BUILDERS, GridIndex
from .ledger import Ledger
from .lifecycle import build_lifecycle, lifecycle_intelligence, policy_summary
from .strategy import EntryRule, ReversionRule
from .validation import (
    EMPTY,
    STRATEGIES,
    Result,
    baseline_results,
    candidates_to_trades,
    evaluate,
    hypothesis_results,
    permutation_test,
    regime_breakdown,
    select_rule,
    walk_forward,
)

MIN_RULE_TRADES = 60

EXIT_POLICIES = {
    "hold_to_expiry": {},
    "tp_5c": {"take_profit": 0.05},
    "tp_10c": {"take_profit": 0.10},
    "tp_15c": {"take_profit": 0.15},
    "tp_10c_sl_10c": {"take_profit": 0.10, "stop_loss": 0.10},
    "tp_15c_sl_10c": {"take_profit": 0.15, "stop_loss": 0.10},
    "sl_10c": {"stop_loss": 0.10},
    "trail_8c": {"trail": 0.08},
    "exit_minute_3": {"time_exit": 3},
    "exit_minute_5": {"time_exit": 5},
}

MIN_OOS_TRADES = 100


def verdict_for(result: Result, permutation: dict, min_trades: int = MIN_OOS_TRADES) -> str:
    """Gross-only gate. Fees are never the reason something fails here.

    Fails CLOSED on a missing permutation p-value. A rule chosen out of
    thousands cannot be called confirmed just because the test that would have
    caught the search never ran; absent evidence is not evidence.
    """
    if result.trades < min_trades:
        return "INSUFFICIENT_DATA"
    if result.edge_ci_low > 0:
        p_value = permutation.get("p_value")
        if p_value is None:
            return "UNTESTED_AGAINST_SEARCH"
        if p_value >= 0.05:
            return "NOT_SIGNIFICANT_AFTER_SEARCH"
        return "GROSS_EDGE_CONFIRMED"
    if result.edge_per_contract > 0:
        return "POSITIVE_BUT_INCONCLUSIVE"
    return "NO_GROSS_EDGE"


def analyse_strategy(
    strategy: str,
    snapshots: list,
    candles: dict,
    closes: dict[str, int],
    deployed_rule,
    ledger: Ledger,
    run_id: str,
    folds: int,
    permutations: int,
    fee: float,
) -> dict:
    # 1. The rule as currently configured, over the whole sample. This path runs
    #    the deployed EntryRule/ReversionRule matching code, not a reimplementation.
    deployed_trades = STRATEGIES[strategy](snapshots, deployed_rule, candles, closes)
    deployed = evaluate(deployed_trades, fee=fee)

    candidates = BUILDERS[strategy](snapshots, candles, closes)
    index = GridIndex(strategy, candidates, min_trades=MIN_RULE_TRADES)
    print(
        f"  {len(candidates)} candidates, {len(index)} rules with >={MIN_RULE_TRADES} trades",
        flush=True,
    )

    # 2. The selection procedure, evaluated out of sample fold by fold.
    folds_detail, oos_trades = walk_forward(index, folds=folds)
    out_of_sample = evaluate(oos_trades, fee=fee)

    # 3. The best rule the full grid can find, and whether the search explains it.
    chosen = select_rule(index, 0, len(candidates), MIN_RULE_TRADES)
    best_rule = chosen.rule if chosen else None
    best_result = (
        evaluate(
            candidates_to_trades(
                candidates,
                index.rule_trades(chosen.position, 0, len(candidates)),
                chosen.take_profit,
            ),
            fee=fee,
        )
        if chosen
        else EMPTY
    )
    permutation = (
        permutation_test(
            index,
            best_result.edge_per_contract,
            min_trades=MIN_RULE_TRADES,
            permutations=permutations,
        )
        if chosen is not None
        else {"permutations": 0, "p_value": None, "note": "no rule met the minimum trade count"}
    )

    # 4. Lifecycles - was a loss ever a win?
    snapshot_index = {(item.ticker, item.elapsed): item for item in snapshots}
    lifecycles, entries = [], []
    source = oos_trades if oos_trades else deployed_trades
    segment = "walk_forward" if oos_trades else "deployed_rule"
    for trade in source:
        snap = snapshot_index.get((trade.ticker, trade.entry_minute))
        if snap is None:
            continue
        life = build_lifecycle(
            ticker=trade.ticker,
            open_ms=trade.open_ms,
            side=trade.side,
            entry_minute=trade.entry_minute,
            entry_price=trade.entry_price,
            settled_won=trade.won,
            candles=candles.get(trade.ticker, []),
            close_ms=closes.get(trade.ticker),
        )
        lifecycles.append(life)
        entries.append((life, snap, segment))
    if entries:
        ledger.record_trades(run_id, strategy, entries)

    policies = {}
    for name, policy in EXIT_POLICIES.items():
        summary = policy_summary(lifecycles, **policy)
        policies[name] = summary
        ledger.record_policy(run_id, f"{strategy}:{name}", summary)

    # 4b. Named rules with no search behind them, so no multiplicity to correct.
    hypotheses = hypothesis_results(index) if strategy == "primary" else []
    for row in hypotheses:
        ledger.record_finding(run_id, f"{strategy}:hypothesis", "rule", row["name"], row)

    # 5. Where does the result concentrate?
    #    When walk-forward produced no trades this falls back to in-sample
    #    deployed-rule trades, which must be stated rather than presented as
    #    out-of-sample evidence.
    regimes = regime_breakdown(source)
    for row in regimes:
        ledger.record_finding(run_id, f"{strategy}:regime", row["dimension"], row["bucket"], row)

    decision = verdict_for(out_of_sample, permutation)
    return {
        "strategy": strategy,
        "verdict": decision,
        "deployed_rule": asdict(deployed_rule),
        "deployed_result": asdict(deployed),
        "walk_forward": {
            "folds": [
                {
                    "index": fold.index,
                    "train_end": _stamp(fold.train_end_ms),
                    "test_end": _stamp(fold.test_end_ms),
                    "rule": fold.rule,
                    "train_trades": fold.train_trades,
                    "train_edge": fold.train_edge,
                    "test_trades": fold.test.trades,
                    "test_edge": round(fold.test.edge_per_contract, 5),
                    "test_roi": round(fold.test.gross_roi, 5),
                }
                for fold in folds_detail
            ],
            "combined": asdict(out_of_sample),
        },
        "best_in_sample_rule": asdict(best_rule) if best_rule else None,
        "best_in_sample_result": asdict(best_result),
        "permutation_test": permutation,
        "analysis_sample": segment,
        "hypothesis_rules": hypotheses,
        "lifecycle": lifecycle_intelligence(lifecycles),
        "exit_policies": policies,
        "regime_findings": regimes,
    }


def _stamp(value: int | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(value / 1000, UTC).isoformat()


def markdown_report(report: dict) -> str:
    lines = [
        "# BTC15 Strategy Validation - Gross Edge",
        "",
        f"Generated: {report['generated_at']}",
        f"Data: {report['coverage']['markets']} settled markets, "
        f"{report['snapshots']} decision snapshots",
        f"Period: {report['period']}",
        "",
        "Fees are excluded from every pass/fail gate below. The question is whether the",
        "strategy beats the price it pays, before costs. Fee-adjusted figures appear",
        "alongside for information only.",
        "",
        "## Edge benchmark - indiscriminate trading on the same markets",
        "",
        "| Baseline | Trades | Win rate | Edge/contract | Gross ROI | 95% CI on edge |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, row in report["baselines"].items():
        lines.append(
            f"| {name.replace('_', ' ')} | {row['trades']} | {row['win_rate']:.2%} | "
            f"${row['edge_per_contract']:.4f} | {row['gross_roi']:.2%} | "
            f"${row['edge_ci_low']:.4f} to ${row['edge_ci_high']:.4f} |"
        )
    lines += [
        "",
        "A calibrated market prices every one of these at zero edge. They are the bar.",
        "",
    ]

    for entry in report["strategies"]:
        combined = entry["walk_forward"]["combined"]
        deployed = entry["deployed_result"]
        lines += [
            f"## {entry['strategy'].title()} strategy - {entry['verdict']}",
            "",
            "| Measure | Trades | Win rate | Edge/contract | Gross ROI | 95% CI on edge |",
            "|---|---:|---:|---:|---:|---:|",
            f"| Deployed rule, full sample | {deployed['trades']} | {deployed['win_rate']:.2%} | "
            f"${deployed['edge_per_contract']:.4f} | {deployed['gross_roi']:.2%} | "
            f"${deployed['edge_ci_low']:.4f} to ${deployed['edge_ci_high']:.4f} |",
            f"| Walk-forward out of sample | {combined['trades']} | {combined['win_rate']:.2%} | "
            f"${combined['edge_per_contract']:.4f} | {combined['gross_roi']:.2%} | "
            f"${combined['edge_ci_low']:.4f} to ${combined['edge_ci_high']:.4f} |",
            "",
        ]
        permutation = entry["permutation_test"]
        if permutation.get("p_value") is not None:
            lines += [
                f"Best rule the full grid found scores ${permutation['observed_edge']:.4f} of edge "
                f"per contract. Shuffling outcomes within price buckets and rerunning the whole "
                f"search produces a median best of ${permutation['null_median_edge']:.4f} and a "
                f"95th percentile of ${permutation['null_95th_edge']:.4f}, "
                f"giving **p = {permutation['p_value']:.3f}** over "
                f"{permutation['permutations']} permutations.",
                "",
            ]

        life = entry["lifecycle"]
        if life.get("trades"):
            lines += [
                "### Trigger to close - was the loss ever a win?",
                "",
                f"Of {life['losers']} losing trades, **{life['rescuable_losses']} "
                f"({life['rescuable_share_of_losses']:.1%})** could have been closed for at least "
                f"2c of profit at some point before expiry. Median best price a loser reached was "
                f"${life['median_loser_mfe']:.4f} above entry.",
                "",
                f"Of {life['winners']} winners, {life['squandered_wins']} "
                f"({life['squandered_share_of_wins']:.1%}) were at least 2c underwater on the way, "
                f"so a tight stop would have cut them.",
                "",
                "| Profit reachable | Losers that got there | Share of losers | Median minute |",
                "|---|---:|---:|---:|",
            ]
            for level, row in life["profit_ladder"].items():
                minute = row["median_minute_reached"]
                lines.append(
                    f"| ${level} | {row['losers_that_reached']} | {row['share_of_losers']:.1%} | "
                    f"{minute if minute is not None else '-'} |"
                )
            lines += [
                "",
                "### Exit policy on identical trades",
                "",
                "Each policy is compared to holding the *same* trades to expiry, paired trade "
                "by trade. `vs hold` is the average dollars per contract gained or lost by "
                "switching, with a 95% interval; a policy only beats holding if that interval "
                "clears zero.",
                "",
                "| Policy | Win rate | Gross ROI | vs hold / contract | 95% CI on vs hold |",
                "|---|---:|---:|---:|---:|",
            ]
            ranked = sorted(
                (row for row in entry["exit_policies"].items() if row[1].get("trades")),
                key=lambda row: row[1].get("vs_hold_per_trade", 0),
                reverse=True,
            )
            for name, row in ranked:
                beats = "**" if row.get("beats_hold") else ""
                lines.append(
                    f"| {beats}{name.replace('_', ' ')}{beats} | {row['win_rate']:.2%} | "
                    f"{row['roi']:.2%} | {row.get('vs_hold_per_trade', 0):+.4f} | "
                    f"{row.get('vs_hold_ci_low', 0):+.4f} to {row.get('vs_hold_ci_high', 0):+.4f} |"
                )
            survivors = [name for name, row in ranked if row.get("beats_hold")]
            lines += [
                "",
                f"{len(ranked)} policies were compared on the same trades, so the best row is "
                "partly a selection artefact even before the interval is read. "
                + (
                    f"Policies whose interval clears zero: {', '.join(survivors)}."
                    if survivors
                    else "No policy's interval clears zero, so none is demonstrably better "
                    "than simply holding to expiry on this sample."
                ),
                "",
                "High win rate is not the same as profit here: a tight take-profit wins nearly "
                "every trade and still loses money, because the occasional full loss outweighs "
                "many small gains. That is the same trap the earlier win-rate-based reports fell "
                "into.",
                "",
            ]

        hypotheses = entry.get("hypothesis_rules") or []
        if hypotheses:
            lines += [
                "### Named rules, no search behind them",
                "",
                "A grid of thousands needs a permutation test because the search itself can",
                "manufacture an edge. These rules were fixed in advance, so they need only an",
                "honest interval and a correction across this short list. Each is split in half",
                "chronologically - an effect in one half and not the other is a regime, not an",
                "edge. They were chosen after inspecting the full-sample calibration curve, so",
                "the two halves are the real evidence, not the total.",
                "",
                "| Rule | Trades | Win rate | Avg price | Edge/contract | 95% CI | 1st half | "
                "2nd half | Verdict |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
            for row in hypotheses:
                both = "**" if row["both_halves_positive"] else ""
                lines.append(
                    f"| {row['name'].replace('_', ' ')} | {row['trades']} | "
                    f"{row['win_rate']:.2%} | {row['average_entry']:.4f} | "
                    f"{both}{row['edge_per_contract']:+.4f}{both} | "
                    f"{row['edge_ci_low']:+.4f} to {row['edge_ci_high']:+.4f} | "
                    f"{row['first_half_edge']:+.4f} | {row['second_half_edge']:+.4f} | "
                    f"{row.get('verdict', '-')} |"
                )
            lines += [
                "",
                "Bold marks a rule positive in *both* halves. A rule that is significant overall "
                "but positive in only one half has not shown a durable edge.",
                "",
            ]

        findings = [row for row in entry["regime_findings"] if row.get("verdict") != "noise"]
        sample_note = (
            "out-of-sample walk-forward trades"
            if entry.get("analysis_sample") == "walk_forward"
            else "IN-SAMPLE deployed-rule trades (walk-forward produced none)"
        )
        lines += [
            f"### Regime findings (10% FDR across all buckets tested; {sample_note})",
            "",
        ]
        if findings:
            lines += [
                "| Dimension | Bucket | Trades | Win rate | Edge/contract | p | Verdict |",
                "|---|---|---:|---:|---:|---:|---|",
            ]
            for row in findings:
                lines.append(
                    f"| {row['dimension']} | {row['bucket']} | {row['trades']} | "
                    f"{row['win_rate']:.2%} | ${row['edge_per_contract']:.4f} | "
                    f"{row['p_value']:.4f} | {row['verdict']} |"
                )
        else:
            tested = len(entry["regime_findings"])
            lines.append(
                f"No regime survived correction ({tested} buckets tested). Every apparent "
                "pocket of performance is consistent with noise."
            )
        lines.append("")

    lines += [
        "## What these numbers cannot show",
        "",
        "- Minute candles cannot prove queue position. A displayed bid is not a guaranteed fill.",
        "- Take-profit fills assume the period's best quote was reachable, which flatters every",
        "  early-exit policy in the table above.",
        "- Settlement uses CF Benchmarks BRTI averaged over the final 60 seconds, not the Binance",
        "  close the features are built from. That basis is a real source of error, measured in",
        "  the snapshot table as `oracle_basis_bps`.",
        "- The permutation test shuffles outcomes independently within price buckets, which",
        "  removes the serial correlation between neighbouring windows. That makes the null",
        "  slightly tighter than reality, so its p-values are, if anything, generous to the",
        "  strategy rather than harsh on it.",
        "- Gross edge is a necessary condition for a live strategy, not a sufficient one. A",
        "  strategy that clears this bar still has to clear fees and slippage afterwards.",
        "",
    ]
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate BTC15 strategies on gross edge")
    parser.add_argument("--cache", default="data/market_data.db")
    parser.add_argument("--ledger", default="data/intelligence.db")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--folds", type=int, default=6)
    parser.add_argument("--permutations", type=int, default=200)
    parser.add_argument(
        "--fee",
        type=float,
        default=0.0,
        help="Reporting only; never gates a verdict. Use -1 for Kalshi's published formula.",
    )
    parser.add_argument("--strategies", default="primary,reversion")
    parser.add_argument("--output", default="reports/validation.json")
    parser.add_argument("--markdown", default="reports/VALIDATION_REPORT.md")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    cache = DataCache(args.cache)
    coverage = cache.coverage()
    markets = cache.markets()
    if not markets:
        raise SystemExit("cache is empty - run scripts/sync_data.py first")

    klines = cache.klines()
    candles = cache.contract_candles()
    closes = {market.ticker: market.close_ms for market in markets}
    print(
        f"{len(markets)} markets, {len(klines)} klines, {len(candles)} tickers with candles",
        flush=True,
    )

    snapshots = build_snapshots(markets, klines, candles)
    print(f"{len(snapshots)} decision snapshots", flush=True)

    ledger = Ledger(args.ledger)
    run_id = args.run_id or datetime.now(UTC).strftime("run-%Y%m%dT%H%M%S")
    period = f"{_stamp(markets[0].open_ms)} to {_stamp(markets[-1].open_ms)}"
    ledger.start_run(
        run_id=run_id,
        strategy=args.strategies,
        fee=args.fee,
        data_start=_stamp(markets[0].open_ms),
        data_end=_stamp(markets[-1].open_ms),
        markets=len(markets),
        snapshots=len(snapshots),
        params=vars(args),
    )
    ledger.record_snapshots(snapshots)

    deployed = {
        "primary": EntryRule.load("strategy.json"),
        "reversion": ReversionRule.load("reversion_strategy.json"),
    }

    results = []
    for strategy in args.strategies.split(","):
        strategy = strategy.strip()
        if strategy not in STRATEGIES:
            continue
        print(f"analysing {strategy}...", flush=True)
        results.append(
            analyse_strategy(
                strategy=strategy,
                snapshots=snapshots,
                candles=candles,
                closes=closes,
                deployed_rule=deployed[strategy],
                ledger=ledger,
                run_id=run_id,
                folds=args.folds,
                permutations=args.permutations,
                fee=args.fee,
            )
        )

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "period": period,
        "coverage": coverage,
        "snapshots": len(snapshots),
        "fee_used_for_reporting": args.fee,
        "gates": "gross edge only; fees never cause a failure here",
        "baselines": {
            name: asdict(result) for name, result in baseline_results(snapshots).items()
        },
        "strategies": results,
    }
    ledger.finish_run(
        run_id,
        ",".join(f"{item['strategy']}={item['verdict']}" for item in results),
        {item["strategy"]: item["verdict"] for item in results},
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    Path(args.markdown).write_text(markdown_report(report), encoding="utf-8")
    print(markdown_report(report))
    print(f"JSON: {output}\nMarkdown: {args.markdown}\nLedger: {args.ledger} (run {run_id})")


if __name__ == "__main__":
    run()
