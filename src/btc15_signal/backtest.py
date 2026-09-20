import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import httpx

from .binance import MarketSnapshot
from .model import Prediction, predict
from .store import wilson_lower


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy_volume: float


@dataclass(frozen=True)
class Trial:
    open_time: int
    target: float
    entry: float
    final: float
    prediction: Prediction
    won: bool


@dataclass(frozen=True)
class Metrics:
    samples: int
    wins: int
    win_rate: float
    lower_95: float
    upper_95: float
    max_losing_streak: int


def fetch_klines(base_url: str, symbol: str, start_ms: int, end_ms: int) -> list[Candle]:
    candles: list[Candle] = []
    cursor = start_ms
    with httpx.Client(timeout=30) as client:
        while cursor < end_ms:
            response = client.get(
                base_url.rstrip("/") + "/api/v3/klines",
                params={
                    "symbol": symbol.upper(),
                    "interval": "1m",
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": 1000,
                },
            )
            response.raise_for_status()
            rows = response.json()
            if not rows:
                break
            candles.extend(
                Candle(
                    open_time=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    taker_buy_volume=float(row[9]),
                )
                for row in rows
            )
            next_cursor = int(rows[-1][0]) + 60_000
            if next_cursor <= cursor:
                raise RuntimeError("Binance pagination stopped advancing")
            cursor = next_cursor
            time.sleep(0.03)
    return candles


def build_trials(candles: list[Candle]) -> list[Trial]:
    groups: dict[int, list[Candle]] = {}
    for candle in candles:
        window_open = candle.open_time - candle.open_time % 900_000
        groups.setdefault(window_open, []).append(candle)
    trials = []
    for opened in sorted(groups):
        rows = sorted(groups[opened], key=lambda item: item.open_time)
        expected = [opened + minute * 60_000 for minute in range(15)]
        if len(rows) != 15 or [row.open_time for row in rows] != expected:
            continue
        entry_rows = rows[:10]
        closes = [row.close for row in entry_rows[-6:]]
        returns = [(b / a - 1) * 10_000 for a, b in pairwise(closes)]
        mean = sum(returns) / len(returns)
        volatility = (sum((value - mean) ** 2 for value in returns) / len(returns)) ** 0.5
        total_volume = sum(row.volume for row in entry_rows)
        buy_volume = sum(row.taker_buy_volume for row in entry_rows)
        sell_volume = max(total_volume - buy_volume, 0)
        taker_imbalance = (buy_volume - sell_volume) / max(total_volume, 1e-9)
        snapshot = MarketSnapshot(
            price=rows[9].close,
            target=rows[0].open,
            bid_imbalance=0.0,
            taker_imbalance=taker_imbalance,
            momentum_5m_bps=(closes[-1] / closes[0] - 1) * 10_000,
            volatility_5m_bps=volatility,
            futures_basis_bps=0.0,
            spread_bps=0.0,
        )
        prediction = predict(snapshot)
        final = rows[14].close
        won = (final >= snapshot.target) == (prediction.side == "UP")
        trials.append(Trial(opened, snapshot.target, snapshot.price, final, prediction, won))
    return trials


def metrics(trials: list[Trial]) -> Metrics:
    samples = len(trials)
    wins = sum(trial.won for trial in trials)
    lower = wilson_lower(wins, samples)
    upper = 1 - wilson_lower(samples - wins, samples)
    losing_streak = max_streak([not trial.won for trial in trials])
    return Metrics(samples, wins, wins / samples if samples else 0.0, lower, upper, losing_streak)


def max_streak(values: list[bool]) -> int:
    best = current = 0
    for value in values:
        current = current + 1 if value else 0
        best = max(best, current)
    return best


def select_threshold(train: list[Trial], target_rate: float, min_samples: int) -> float | None:
    candidates = []
    for point in range(50, 100):
        threshold = point / 100
        selected = [t for t in train if t.prediction.raw_probability >= threshold]
        result = metrics(selected)
        if result.samples >= min_samples and result.lower_95 >= target_rate:
            candidates.append((result.samples, threshold))
    return max(candidates)[1] if candidates else None


def evaluate(trials: list[Trial], threshold: float | None) -> Metrics:
    if threshold is None:
        return metrics([])
    return metrics([trial for trial in trials if trial.prediction.raw_probability >= threshold])


def period(trials: list[Trial]) -> str:
    if not trials:
        return "n/a"
    start = datetime.fromtimestamp(trials[0].open_time / 1000, UTC)
    end = datetime.fromtimestamp(trials[-1].open_time / 1000, UTC)
    return f"{start:%Y-%m-%d} to {end:%Y-%m-%d} UTC"


def make_report(trials: list[Trial], days: int, target_rate: float, min_samples: int) -> dict:
    train_end = int(len(trials) * 0.60)
    validation_end = int(len(trials) * 0.80)
    train = trials[:train_end]
    validation = trials[train_end:validation_end]
    holdout = trials[validation_end:]
    threshold = select_threshold(train, target_rate, min_samples)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "requested_days": days,
        "period": period(trials),
        "method": {
            "entry": "Close of minute 10; five minutes remain",
            "target": "Open of the aligned 15-minute UTC window",
            "outcome": "Close of minute 15 remains on predicted side",
            "split": "Chronological 60% train / 20% validation / 20% untouched holdout",
            "selection": (
                f"Largest-coverage threshold with train 95% lower bound >= {target_rate:.0%}"
            ),
            "limitations": [
                "Historical top-of-book depth and futures basis are unavailable in kline data.",
                "Results measure direction accuracy, not profit; contract prices and fees "
                "are absent.",
                "A Binance close may differ from the contract settlement oracle.",
            ],
        },
        "threshold": threshold,
        "all_windows": asdict(metrics(trials)),
        "train": {"period": period(train), **asdict(evaluate(train, threshold))},
        "validation": {"period": period(validation), **asdict(evaluate(validation, threshold))},
        "holdout": {"period": period(holdout), **asdict(evaluate(holdout, threshold))},
    }


def markdown(report: dict) -> str:
    threshold = report["threshold"]
    threshold_text = f"{threshold:.0%}" if threshold is not None else "No qualifying threshold"
    lines = [
        "# BTC15 Strategy Backtest",
        "",
        f"Generated: {report['generated_at']}",
        f"Period: {report['period']}",
        f"Selected raw-probability threshold: **{threshold_text}**",
        "",
        "| Segment | Signals | Wins | Win rate | 95% lower bound | Max losing streak |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in ("train", "validation", "holdout"):
        row = report[name]
        lines.append(
            f"| {name.title()} | {row['samples']} | {row['wins']} | "
            f"{row['win_rate']:.2%} | {row['lower_95']:.2%} | {row['max_losing_streak']} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "A strategy is not validated at the requested level unless the untouched holdout has "
            "enough signals and its 95% lower confidence bound is at least 80%.",
            "",
            "## Limitations",
            "",
            *(f"- {item}" for item in report["method"]["limitations"]),
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest the BTC 15-minute signal")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--base-url", default="https://data-api.binance.vision")
    parser.add_argument("--target-rate", type=float, default=0.80)
    parser.add_argument("--min-train-samples", type=int, default=200)
    parser.add_argument("--output", default="reports/backtest.json")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    if args.days < 7:
        raise SystemExit("--days must be at least 7")
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    candles = fetch_klines(
        args.base_url, args.symbol, int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    )
    trials = build_trials(candles)
    report = make_report(trials, args.days, args.target_rate, args.min_train_samples)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    md_output = output.with_suffix(".md")
    md_output.write_text(markdown(report))
    print(markdown(report))
    print(f"JSON: {output}\nMarkdown: {md_output}")


if __name__ == "__main__":
    run()
