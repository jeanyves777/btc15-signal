import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import httpx

from .backtest import Candle, fetch_klines
from .snapshot import MarketSnapshot
from .kalshi import iso_ms
from .model import predict
from .store import wilson_lower
from .strategy import EntryRule


@dataclass(frozen=True)
class Candidate:
    open_ms: int
    ticker: str
    remaining_minutes: int
    side: str
    raw_probability: float
    ask: float
    normalized_distance: float
    momentum_aligned: bool
    won: bool
    pnl: float


@dataclass(frozen=True)
class Metrics:
    signals: int
    wins: int
    win_rate: float
    lower_95_win_rate: float
    average_ask: float
    total_cost: float
    pnl_after_fee_buffer: float
    roi_after_fee_buffer: float
    conservative_profit_edge: float
    max_drawdown: float


def fetch_settled_markets(base_url: str, series: str, start_ms: int, end_ms: int) -> list[dict]:
    markets = []
    cursor = None
    with httpx.Client(timeout=30) as client:
        while True:
            params = {"series_ticker": series, "status": "settled", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            response = client.get(base_url.rstrip("/") + "/markets", params=params)
            response.raise_for_status()
            payload = response.json()
            page = payload.get("markets", [])
            markets.extend(
                market
                for market in page
                if start_ms <= iso_ms(market["open_time"]) < end_ms
                and market.get("floor_strike") is not None
            )
            if not page or min(iso_ms(item["open_time"]) for item in page) < start_ms:
                break
            cursor = payload.get("cursor")
            if not cursor:
                break
    return sorted(markets, key=lambda item: iso_ms(item["open_time"]))


def fetch_contract_candles(base_url: str, markets: list[dict]) -> dict[str, list[dict]]:
    result = {}
    with httpx.Client(timeout=30) as client:
        for offset in range(0, len(markets), 10):
            batch = markets[offset : offset + 10]
            start = min(iso_ms(item["open_time"]) for item in batch) // 1000
            end = max(iso_ms(item["close_time"]) for item in batch) // 1000
            for attempt in range(5):
                response = client.get(
                    base_url.rstrip("/") + "/markets/candlesticks",
                    params={
                        "market_tickers": ",".join(item["ticker"] for item in batch),
                        "start_ts": start,
                        "end_ts": end,
                        "period_interval": 1,
                    },
                )
                if response.status_code < 500 and response.status_code != 429:
                    break
                time.sleep(0.5 * (attempt + 1))
            response.raise_for_status()
            for item in response.json().get("markets", []):
                result[item["market_ticker"]] = item.get("candlesticks", [])
    return result


def group_binance(candles: list[Candle]) -> dict[int, list[Candle]]:
    groups = {}
    for candle in candles:
        opened = candle.open_time - candle.open_time % 900_000
        groups.setdefault(opened, []).append(candle)
    return {key: sorted(value, key=lambda item: item.open_time) for key, value in groups.items()}


def contract_ask(candles: list[dict], cutoff: int, side: str) -> float | None:
    eligible = [item for item in candles if item["end_period_ts"] <= cutoff]
    if not eligible:
        return None
    candle = max(eligible, key=lambda item: item["end_period_ts"])
    yes_ask = candle.get("yes_ask", {}).get("close_dollars")
    yes_bid = candle.get("yes_bid", {}).get("close_dollars")
    if yes_ask is None or yes_bid is None:
        return None
    return float(yes_ask) if side == "UP" else 1 - float(yes_bid)


def snapshot_at(rows: list[Candle], target: float, elapsed: int) -> MarketSnapshot | None:
    if len(rows) != 15 or elapsed < 3:
        return None
    history = rows[:elapsed]
    closes = [row.close for row in history[-6:]]
    returns = [(right / left - 1) * 10_000 for left, right in pairwise(closes)]
    mean = sum(returns) / len(returns)
    volatility = (sum((item - mean) ** 2 for item in returns) / len(returns)) ** 0.5
    volume = sum(row.volume for row in history)
    buys = sum(row.taker_buy_volume for row in history)
    taker_imbalance = (2 * buys - volume) / max(volume, 1e-9)
    return MarketSnapshot(
        price=history[-1].close,
        target=target,
        bid_imbalance=0.0,
        taker_imbalance=taker_imbalance,
        momentum_5m_bps=(closes[-1] / closes[0] - 1) * 10_000,
        volatility_5m_bps=volatility,
        futures_basis_bps=0.0,
        spread_bps=0.0,
        window_high=max(row.high for row in history),
        window_low=min(row.low for row in history),
        elapsed_minutes=elapsed,
    )


def build_candidates(
    binance: list[Candle], markets: list[dict], contract_data: dict[str, list[dict]]
) -> list[Candidate]:
    groups = group_binance(binance)
    candidates = []
    for market in markets:
        opened = iso_ms(market["open_time"])
        rows = groups.get(opened, [])
        target = float(market["floor_strike"])
        for remaining in range(3, 8):
            elapsed = 15 - remaining
            snapshot = snapshot_at(rows, target, elapsed)
            if snapshot is None:
                continue
            prediction = predict(snapshot)
            ask = contract_ask(
                contract_data.get(market["ticker"], []),
                opened // 1000 + elapsed * 60,
                prediction.side,
            )
            if ask is None or not 0 < ask < 1:
                continue
            expected = "yes" if prediction.side == "UP" else "no"
            won = market.get("result") == expected
            direction = 1 if prediction.side == "UP" else -1
            candidates.append(
                Candidate(
                    open_ms=opened,
                    ticker=market["ticker"],
                    remaining_minutes=remaining,
                    side=prediction.side,
                    raw_probability=prediction.raw_probability,
                    ask=ask,
                    normalized_distance=(
                        prediction.distance_bps / max(snapshot.volatility_5m_bps, 1.0)
                    ),
                    momentum_aligned=direction * snapshot.momentum_5m_bps > 0,
                    won=won,
                    pnl=(1 - ask if won else -ask),
                )
            )
    return candidates


def apply_rule(candidates: list[Candidate], rule: EntryRule) -> list[Candidate]:
    return [
        item
        for item in candidates
        if item.remaining_minutes == rule.remaining_minutes
        and item.raw_probability >= rule.min_raw_probability
        and rule.min_ask <= item.ask <= rule.max_ask
        and item.normalized_distance >= rule.min_normalized_distance
        and (not rule.require_momentum_alignment or item.momentum_aligned)
    ]


def metrics(items: list[Candidate], fee: float) -> Metrics:
    wins = sum(item.won for item in items)
    cost = sum(item.ask for item in items)
    net = sum(item.pnl - fee for item in items)
    win_rate = wins / len(items) if items else 0.0
    lower = wilson_lower(wins, len(items))
    average_ask = cost / len(items) if items else 0.0
    equity = peak = drawdown = 0.0
    for item in items:
        equity += item.pnl - fee
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return Metrics(
        signals=len(items),
        wins=wins,
        win_rate=win_rate,
        lower_95_win_rate=lower,
        average_ask=average_ask,
        total_cost=cost,
        pnl_after_fee_buffer=net,
        roi_after_fee_buffer=net / cost if cost else 0.0,
        conservative_profit_edge=lower - average_ask - fee if items else 0.0,
        max_drawdown=drawdown,
    )


def rule_grid(fee: float) -> list[EntryRule]:
    rules = []
    for remaining in range(3, 8):
        for raw in (0.50, 0.70, 0.80, 0.90):
            for minimum_distance in (0.0, 0.5, 1.0, 2.0):
                for min_ask in (0.40, 0.50, 0.60, 0.70, 0.80):
                    for max_ask in (0.75, 0.80, 0.85, 0.90, 0.95):
                        if min_ask >= max_ask:
                            continue
                        for alignment in (False, True):
                            rules.append(
                                EntryRule(
                                    enabled=False,
                                    remaining_minutes=remaining,
                                    min_raw_probability=raw,
                                    min_ask=min_ask,
                                    max_ask=max_ask,
                                    min_normalized_distance=minimum_distance,
                                    require_momentum_alignment=alignment,
                                    fee_buffer=fee,
                                )
                            )
    return rules


def optimize(train: list[Candidate], validation: list[Candidate], fee: float) -> EntryRule | None:
    finalists = []
    for rule in rule_grid(fee):
        train_result = metrics(apply_rule(train, rule), fee)
        if (
            train_result.signals < 100
            or train_result.win_rate < 0.80
            or train_result.conservative_profit_edge <= 0
        ):
            continue
        validation_result = metrics(apply_rule(validation, rule), fee)
        if (
            validation_result.signals >= 40
            and validation_result.win_rate >= 0.80
            and validation_result.conservative_profit_edge > 0
        ):
            finalists.append((validation_result.pnl_after_fee_buffer, rule))
    return max(finalists, key=lambda item: item[0])[1] if finalists else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize the BTC15 Kalshi entry rule")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--fee-buffer", type=float, default=0.02)
    parser.add_argument("--output", default="reports/flexible-backtest.json")
    parser.add_argument("--strategy-output", default="strategy.json")
    parser.add_argument("--kalshi-url", default="https://api.elections.kalshi.com/trade-api/v2")
    parser.add_argument("--binance-url", default="https://data-api.binance.vision")
    return parser.parse_args()


def write_json_atomic(path: str | Path, payload: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(destination)


def run() -> None:
    args = parse_args()
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    markets = fetch_settled_markets(args.kalshi_url, "KXBTC15M", start_ms, end_ms)
    contract_data = fetch_contract_candles(args.kalshi_url, markets)
    binance = fetch_klines(args.binance_url, "BTCUSDT", start_ms, end_ms)
    candidates = build_candidates(binance, markets, contract_data)
    train_end = start_ms + (end_ms - start_ms) // 2
    validation_end = start_ms + 3 * (end_ms - start_ms) // 4
    train = [item for item in candidates if item.open_ms < train_end]
    validation = [item for item in candidates if train_end <= item.open_ms < validation_end]
    holdout = [item for item in candidates if item.open_ms >= validation_end]
    selected = optimize(train, validation, args.fee_buffer)
    holdout_result = (
        metrics(apply_rule(holdout, selected), args.fee_buffer)
        if selected
        else metrics([], args.fee_buffer)
    )
    validated = (
        selected is not None
        and holdout_result.signals >= 30
        and holdout_result.win_rate >= 0.80
        and holdout_result.conservative_profit_edge > 0
    )
    final_rule = EntryRule(**{**asdict(selected or EntryRule()), "enabled": validated})
    selected_train = apply_rule(train, selected) if selected else []
    selected_validation = apply_rule(validation, selected) if selected else []
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "period": f"{start:%Y-%m-%d} to {end:%Y-%m-%d} UTC",
        "exact_kalshi_targets": True,
        "markets": len(markets),
        "candidate_snapshots": len(candidates),
        "validated_for_live_entry": validated,
        "selected_rule": asdict(selected) if selected else None,
        "deployed_rule": asdict(final_rule),
        "train": asdict(metrics(selected_train, args.fee_buffer)),
        "validation": asdict(metrics(selected_validation, args.fee_buffer)),
        "holdout": asdict(holdout_result),
    }
    write_json_atomic(args.output, report)
    write_json_atomic(args.strategy_output, asdict(final_rule))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run()
