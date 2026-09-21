"""Decision snapshots and regime labels.

One row per (market, decision minute). Everything here is computed from data
that existed at the decision instant. Regime thresholds are rolling quantiles
over a *trailing* window rather than full-sample quantiles, so a regime label
is something the live service could also compute.
"""

from bisect import bisect_left
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise

from .datasource import Candle, ContractCandle, Market

WINDOW_MS = 900_000
TRAILING_WINDOW = 96 * 7  # one week of 15-minute markets


@dataclass(frozen=True)
class Snapshot:
    """What was knowable at `elapsed` minutes into a 15-minute market."""

    ticker: str
    open_ms: int
    elapsed: int
    remaining: int
    target: float
    price: float
    signed_distance_bps: float
    momentum_5m_bps: float
    volatility_5m_bps: float
    normalized_distance: float
    taker_imbalance: float
    window_high: float
    window_low: float
    window_range_bps: float
    trailing_vol_bps: float
    trailing_trend_bps: float
    yes_bid: float | None
    yes_ask: float | None
    spread: float | None
    contract_volume: float
    open_interest: float
    result: str | None
    expiration_value: float | None
    settle_distance_bps: float | None
    binance_close: float | None
    oracle_basis_bps: float | None

    # regime labels
    session: str = ""
    weekday: str = ""
    vol_regime: str = ""
    trend_regime: str = ""
    liquidity_regime: str = ""
    distance_regime: str = ""

    def entry_price(self, side: str) -> float | None:
        """Executable ask for the requested side."""
        if side == "UP":
            return self.yes_ask
        return None if self.yes_bid is None else round(1 - self.yes_bid, 4)

    def won(self, side: str) -> bool | None:
        if self.result not in {"yes", "no"}:
            return None
        return self.result == ("yes" if side == "UP" else "no")


def group_klines(candles: list[Candle]) -> dict[int, list[Candle]]:
    groups: dict[int, list[Candle]] = {}
    for candle in candles:
        groups.setdefault(candle.open_time - candle.open_time % WINDOW_MS, []).append(candle)
    for rows in groups.values():
        rows.sort(key=lambda item: item.open_time)
    return groups


def quote_at(candles: list[ContractCandle], cutoff_ts: int) -> ContractCandle | None:
    """Last contract candle that had fully closed at `cutoff_ts`."""
    index = bisect_left([candle.end_period_ts for candle in candles], cutoff_ts + 1)
    return candles[index - 1] if index else None


class TrailingStats:
    """Rolling quantiles over a trailing window of completed markets."""

    def __init__(self, size: int = TRAILING_WINDOW) -> None:
        self.size = size
        self.values: dict[str, deque[float]] = {}

    def label(self, key: str, value: float, low: float = 1 / 3, high: float = 2 / 3) -> str:
        history = self.values.setdefault(key, deque(maxlen=self.size))
        if len(history) < 30:
            band = "warmup"
        else:
            ordered = sorted(history)
            lower = ordered[int(len(ordered) * low)]
            upper = ordered[int(len(ordered) * high)]
            band = "low" if value <= lower else ("high" if value >= upper else "mid")
        history.append(value)
        return band


def _session(open_ms: int) -> str:
    hour = datetime.fromtimestamp(open_ms / 1000, UTC).hour
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 13:
        return "europe"
    if 13 <= hour < 21:
        return "us"
    return "late-us"


def _weekday(open_ms: int) -> str:
    return datetime.fromtimestamp(open_ms / 1000, UTC).strftime("%a")


def _trailing_context(
    klines: list[Candle], index: dict[int, int], open_ms: int
) -> tuple[float, float]:
    """Realized volatility and trend over the four hours before the window opened."""
    position = index.get(open_ms)
    if position is None or position < 241:
        return 0.0, 0.0
    history = klines[position - 240 : position]
    closes = [row.close for row in history]
    returns = [(b / a - 1) * 10_000 for a, b in pairwise(closes)]
    if not returns:
        return 0.0, 0.0
    mean = sum(returns) / len(returns)
    vol = (sum((value - mean) ** 2 for value in returns) / len(returns)) ** 0.5
    trend = (closes[-1] / closes[0] - 1) * 10_000
    return vol, trend


def build_snapshots(
    markets: list[Market],
    klines: list[Candle],
    contract_candles: dict[str, list[ContractCandle]],
    minutes: range = range(1, 15),
) -> list[Snapshot]:
    """One snapshot per (market, decision minute), oldest first."""
    groups = group_klines(klines)
    kline_index = {candle.open_time: position for position, candle in enumerate(klines)}
    stats = TrailingStats()
    snapshots: list[Snapshot] = []

    for market in sorted(markets, key=lambda item: item.open_ms):
        rows = groups.get(market.open_ms, [])
        expected = {market.open_ms + minute * 60_000 for minute in range(15)}
        available = {row.open_time: row for row in rows}
        candles = contract_candles.get(market.ticker, [])
        trailing_vol, trailing_trend = _trailing_context(klines, kline_index, market.open_ms)
        final = available.get(market.open_ms + 14 * 60_000)
        binance_close = final.close if final else None
        oracle_basis = None
        if binance_close and market.expiration_value:
            oracle_basis = (binance_close / market.expiration_value - 1) * 10_000

        vol_regime = stats.label("vol", trailing_vol)
        trend_regime = stats.label("trend", abs(trailing_trend))

        for elapsed in minutes:
            history = [
                available[market.open_ms + minute * 60_000]
                for minute in range(elapsed)
                if market.open_ms + minute * 60_000 in available
            ]
            if len(history) < max(3, elapsed) or len(available) != len(expected):
                continue
            closes = [row.close for row in history[-6:]]
            returns = [(b / a - 1) * 10_000 for a, b in pairwise(closes)]
            mean = sum(returns) / len(returns) if returns else 0.0
            volatility = (
                (sum((value - mean) ** 2 for value in returns) / len(returns)) ** 0.5
                if returns
                else 0.0
            )
            volume = sum(row.volume for row in history)
            buys = sum(row.taker_buy_volume for row in history)
            price = history[-1].close
            high = max(row.high for row in history)
            low = min(row.low for row in history)
            signed_distance = (price / market.floor_strike - 1) * 10_000
            cutoff = market.open_ms // 1000 + elapsed * 60
            quote = quote_at(candles, cutoff)
            # Liquidity must be what had traded BY this minute. The market's
            # settled total volume is roughly 44% future at minute 10, and
            # labelling snapshots with it manufactures regime "findings".
            traded = sum(
                item.volume for item in candles if item.end_period_ts <= cutoff
            )
            liquidity_regime = stats.label(f"liquidity{elapsed}", traded)
            settle_distance = (
                (market.expiration_value / market.floor_strike - 1) * 10_000
                if market.expiration_value
                else None
            )
            snapshots.append(
                Snapshot(
                    ticker=market.ticker,
                    open_ms=market.open_ms,
                    elapsed=elapsed,
                    remaining=15 - elapsed,
                    target=market.floor_strike,
                    price=price,
                    signed_distance_bps=signed_distance,
                    momentum_5m_bps=(closes[-1] / closes[0] - 1) * 10_000,
                    volatility_5m_bps=volatility,
                    normalized_distance=abs(signed_distance) / max(volatility, 1.0),
                    taker_imbalance=(2 * buys - volume) / max(volume, 1e-9),
                    window_high=high,
                    window_low=low,
                    window_range_bps=(high / low - 1) * 10_000 if low else 0.0,
                    trailing_vol_bps=trailing_vol,
                    trailing_trend_bps=trailing_trend,
                    yes_bid=quote.yes_bid_close if quote else None,
                    yes_ask=quote.yes_ask_close if quote else None,
                    spread=(
                        round(quote.yes_ask_close - quote.yes_bid_close, 4)
                        if quote and quote.yes_ask_close is not None and quote.yes_bid_close
                        is not None
                        else None
                    ),
                    contract_volume=traded,
                    open_interest=quote.open_interest if quote else 0.0,
                    result=market.result,
                    expiration_value=market.expiration_value,
                    settle_distance_bps=settle_distance,
                    binance_close=binance_close,
                    oracle_basis_bps=oracle_basis,
                    session=_session(market.open_ms),
                    weekday=_weekday(market.open_ms),
                    vol_regime=vol_regime,
                    trend_regime=trend_regime,
                    liquidity_regime=liquidity_regime,
                    distance_regime=(
                        "far"
                        if abs(signed_distance) >= 20
                        else ("near" if abs(signed_distance) >= 6 else "atm")
                    ),
                )
            )
    return snapshots
