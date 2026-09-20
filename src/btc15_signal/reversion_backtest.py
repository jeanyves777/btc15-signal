import argparse
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .backtest import Candle, fetch_klines
from .kalshi import iso_ms
from .kalshi_backtest import (
    contract_ask,
    fetch_contract_candles,
    fetch_settled_markets,
    group_binance,
    snapshot_at,
)
from .store import wilson_lower
from .strategy import ReversionRule


@dataclass(frozen=True)
class ReversionCandidate:
    open_ms: int
    ticker: str
    remaining_minutes: int
    side: str
    entry_price: float
    spike_bps: float
    rejection_bps: float
    distance_bps: float
    settlement_won: bool
    future_bid_highs: tuple[float, ...]


@dataclass(frozen=True)
class ReversionTrade:
    candidate: ReversionCandidate
    take_profit_hit: bool
    pnl_after_fee: float


@dataclass(frozen=True)
class ReversionMetrics:
    trades: int
    profitable: int
    win_rate: float
    lower_95_win_rate: float
    take_profit_hits: int
    take_profit_rate: float
    average_entry: float
    total_pnl_after_fee: float
    roi_after_fee: float
    max_drawdown: float


def _dollars(candle: dict, quote: str, field: str) -> float | None:
    value = candle.get(quote, {}).get(field + "_dollars")
    return float(value) if value is not None else None


def future_bid_highs(candles: list[dict], cutoff: int, side: str) -> tuple[float, ...]:
    prices = []
    for candle in candles:
        if candle["end_period_ts"] <= cutoff:
            continue
        if side == "UP":
            price = _dollars(candle, "yes_bid", "high")
        else:
            yes_ask_low = _dollars(candle, "yes_ask", "low")
            price = 1 - yes_ask_low if yes_ask_low is not None else None
        if price is not None:
            prices.append(price)
    return tuple(prices)


def build_reversion_candidates(
    binance: list[Candle], markets: list[dict], contract_data: dict[str, list[dict]]
) -> list[ReversionCandidate]:
    groups = group_binance(binance)
    candidates = []
    broad_rule = ReversionRule(
        min_spike_bps=0,
        min_rejection_bps=0,
        max_rejection_bps=100,
        max_distance_bps=100,
    )
    for market in markets:
        opened = iso_ms(market["open_time"])
        rows = groups.get(opened, [])
        target = float(market["floor_strike"])
        for remaining in range(12, 9, -1):
            elapsed = 15 - remaining
            snapshot = snapshot_at(rows, target, elapsed)
            if snapshot is None:
                continue
            setup = broad_rule.setup(snapshot, remaining)
            if setup is None:
                continue
            cutoff = opened // 1000 + elapsed * 60
            entry = contract_ask(contract_data.get(market["ticker"], []), cutoff, setup.side)
            if entry is None or not 0 < entry < 1:
                continue
            candidates.append(
                ReversionCandidate(
                    opened,
                    market["ticker"],
                    remaining,
                    setup.side,
                    entry,
                    setup.spike_bps,
                    setup.rejection_bps,
                    setup.distance_bps,
                    market.get("result") == ("yes" if setup.side == "UP" else "no"),
                    future_bid_highs(contract_data.get(market["ticker"], []), cutoff, setup.side),
                )
            )
    return candidates


def apply_reversion_rule(
    candidates: list[ReversionCandidate], rule: ReversionRule
) -> list[ReversionTrade]:
    trades = []
    seen = set()
    for item in sorted(candidates, key=lambda row: (row.open_ms, -row.remaining_minutes)):
        if item.ticker in seen:
            continue
        if not (
            rule.min_remaining_minutes <= item.remaining_minutes <= rule.max_remaining_minutes
            and rule.min_entry_price <= item.entry_price <= rule.max_entry_price
            and item.spike_bps >= rule.min_spike_bps
            and rule.min_rejection_bps <= item.rejection_bps <= rule.max_rejection_bps
            and item.distance_bps <= rule.max_distance_bps
        ):
            continue
        seen.add(item.ticker)
        hit = any(price >= rule.take_profit_price for price in item.future_bid_highs)
        gross_pnl = (
            rule.take_profit_price - item.entry_price
            if hit
            else (1 - item.entry_price if item.settlement_won else -item.entry_price)
        )
        trades.append(ReversionTrade(item, hit, gross_pnl - rule.fee_buffer))
    return trades


def reversion_metrics(trades: list[ReversionTrade]) -> ReversionMetrics:
    profitable = sum(item.pnl_after_fee > 0 for item in trades)
    take_profit_hits = sum(item.take_profit_hit for item in trades)
    cost = sum(item.candidate.entry_price for item in trades)
    pnl = sum(item.pnl_after_fee for item in trades)
    equity = peak = drawdown = 0.0
    for item in trades:
        equity += item.pnl_after_fee
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    count = len(trades)
    return ReversionMetrics(
        count,
        profitable,
        profitable / count if count else 0,
        wilson_lower(profitable, count),
        take_profit_hits,
        take_profit_hits / count if count else 0,
        cost / count if count else 0,
        pnl,
        pnl / cost if cost else 0,
        drawdown,
    )


def rule_grid(fee: float) -> list[ReversionRule]:
    return [
        ReversionRule(
            min_spike_bps=spike,
            min_rejection_bps=rejection,
            max_rejection_bps=maximum_rejection,
            max_distance_bps=distance,
            take_profit_price=take_profit,
            fee_buffer=fee,
        )
        for spike in (4.0, 8.0, 12.0, 16.0)
        for rejection in (0.5, 1.0, 2.0, 4.0)
        for maximum_rejection in (8.0, 12.0, 20.0)
        if rejection < maximum_rejection
        for distance in (15.0, 25.0, 35.0, 50.0)
        for take_profit in (0.40, 0.45, 0.50, 0.55)
    ]


def optimize_reversion(
    train: list[ReversionCandidate], validation: list[ReversionCandidate], fee: float
) -> ReversionRule | None:
    finalists = []
    for rule in rule_grid(fee):
        train_result = reversion_metrics(apply_reversion_rule(train, rule))
        if train_result.trades < 60 or train_result.total_pnl_after_fee <= 0:
            continue
        validation_result = reversion_metrics(apply_reversion_rule(validation, rule))
        if validation_result.trades >= 20 and validation_result.total_pnl_after_fee > 0:
            finalists.append((validation_result.total_pnl_after_fee, rule))
    return max(finalists, key=lambda item: item[0])[1] if finalists else None


def write_json_atomic(path: str | Path, payload: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(destination)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest BTC15 spike-reversion take-profit")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--fee-buffer", type=float, default=0.02)
    parser.add_argument("--output", default="reports/reversion-backtest.json")
    parser.add_argument("--strategy-output", default="reversion_strategy.json")
    parser.add_argument("--kalshi-url", default="https://external-api.kalshi.com/trade-api/v2")
    parser.add_argument("--binance-url", default="https://data-api.binance.vision")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    markets = fetch_settled_markets(args.kalshi_url, "KXBTC15M", start_ms, end_ms)
    contract_data = fetch_contract_candles(args.kalshi_url, markets)
    binance = fetch_klines(args.binance_url, "BTCUSDT", start_ms, end_ms)
    candidates = build_reversion_candidates(binance, markets, contract_data)
    train_end = start_ms + (end_ms - start_ms) // 2
    validation_end = start_ms + 3 * (end_ms - start_ms) // 4
    train = [item for item in candidates if item.open_ms < train_end]
    validation = [item for item in candidates if train_end <= item.open_ms < validation_end]
    holdout = [item for item in candidates if item.open_ms >= validation_end]
    selected = optimize_reversion(train, validation, args.fee_buffer)
    holdout_result = (
        reversion_metrics(apply_reversion_rule(holdout, selected))
        if selected
        else reversion_metrics([])
    )
    validated = bool(
        selected
        and holdout_result.trades >= 20
        and holdout_result.total_pnl_after_fee > 0
        and holdout_result.lower_95_win_rate >= 0.50
    )
    deployed = replace(selected or ReversionRule(), enabled=validated)
    baseline = ReversionRule(fee_buffer=args.fee_buffer)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "period": f"{start:%Y-%m-%d} to {end:%Y-%m-%d} UTC",
        "method": (
            "30-35 cent contrarian entry after spike rejection; resting take-profit or settlement"
        ),
        "markets": len(markets),
        "candidate_snapshots": len(candidates),
        "baseline_rule": asdict(baseline),
        "baseline_all": asdict(reversion_metrics(apply_reversion_rule(candidates, baseline))),
        "baseline_train": asdict(reversion_metrics(apply_reversion_rule(train, baseline))),
        "baseline_validation": asdict(
            reversion_metrics(apply_reversion_rule(validation, baseline))
        ),
        "baseline_holdout": asdict(reversion_metrics(apply_reversion_rule(holdout, baseline))),
        "validated_for_live_entry": validated,
        "selected_rule": asdict(selected) if selected else None,
        "deployed_rule": asdict(deployed),
        "train": asdict(reversion_metrics(apply_reversion_rule(train, selected)))
        if selected
        else asdict(reversion_metrics([])),
        "validation": asdict(reversion_metrics(apply_reversion_rule(validation, selected)))
        if selected
        else asdict(reversion_metrics([])),
        "holdout": asdict(holdout_result),
        "limitations": [
            "Minute candles cannot prove queue position or that every displayed price filled.",
            "Fee buffer is conservative but not an account-specific fee calculation.",
            "Binance prices can differ from the contract settlement oracle.",
        ],
    }
    write_json_atomic(args.output, report)
    write_json_atomic(args.strategy_output, asdict(deployed))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run()
