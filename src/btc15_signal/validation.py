"""Strategy validation, judged on gross edge.

Fees are deliberately excluded from every gate here. The question this module
answers is the prior one: *before any transaction cost, does the strategy have
an edge over the price it pays?* A strategy with no gross edge can never be
rescued by a better fee tier, so there is no point discussing fees until this
question is settled. Net numbers are still computed and reported alongside, as
information rather than as a pass/fail criterion.

The central quantity is **edge per contract** - mean realised P&L per contract,
scored on the exit each strategy actually defines:

    pnl  = payoff - entry_price
    payoff = take_profit   if the strategy's exit would have filled before close
           = 1 if the market settled the strategy's way, else 0

The reversion strategy buys at 30-35c intending to sell at a take-profit, so it
is scored on that sale; settlement only decides the trades whose offer never
filled. The non-return strategy really does hold to expiry, so for it the two
definitions coincide. Scoring a take-profit strategy at settlement measures a
strategy nobody trades.

Zero remains the null. Under an efficient market the contract price is a
martingale, so by optional stopping *every* exit rule has zero expected P&L; a
contract bought at its true probability and sold at any stopping time still
breaks even. Gross ROI is the same quantity divided by average cost.

Three things guard against fooling ourselves:

* **Walk-forward** evaluates the *selection procedure* - refit the grid on
  everything before a fold, trade the fold, move on - rather than reporting one
  rule that was chosen with hindsight over the whole sample.
* **Moving-block bootstrap** builds confidence intervals that survive the serial
  correlation between consecutive 15-minute windows, which an i.i.d. Wilson
  interval on win rate ignores.
* **Within-price-bucket permutation** gives a p-value for the best rule found by
  the whole grid search. Shuffling outcomes inside narrow price buckets keeps the
  market's own price/probability calibration intact and destroys only the link
  between the strategy's features and the result, which is precisely the edge
  being claimed.
"""

import math
import random
from dataclasses import asdict, dataclass

from .snapshot import MarketSnapshot
from .features import Snapshot
from .gridsearch import MATCHERS, Candidate, GridIndex, max_exit_after
from .model import predict
from .strategy import EntryRule, ReversionRule

BLOCK_SIZE = 8  # two hours of consecutive 15-minute markets
BOOTSTRAP_SAMPLES = 2000
PERMUTATIONS = 500
PRICE_BUCKET = 0.05


# --------------------------------------------------------------------- trades


@dataclass(frozen=True)
class Trade:
    """One taken trade, scored on the exit the strategy actually defines."""

    ticker: str
    open_ms: int
    side: str
    entry_minute: int
    entry_price: float
    won: bool
    pnl: float | None = None
    took_profit: bool = False
    session: str = ""
    vol_regime: str = ""
    trend_regime: str = ""
    liquidity_regime: str = ""
    distance_regime: str = ""

    def __post_init__(self) -> None:
        # Defaulting to zero would let a caller that forgets the exit silently
        # score every trade as a scratch. Settlement is the honest default.
        if self.pnl is None:
            object.__setattr__(self, "pnl", self.settlement_pnl)

    @property
    def settlement_pnl(self) -> float:
        """What holding this same trade to expiry would have paid."""
        return (1 - self.entry_price) if self.won else -self.entry_price


FEE_RATE = 0.07


def kalshi_fee(price: float, contracts: float = 1) -> float:
    """Kalshi's published trading fee for one ORDER: ceil(0.07 * C * P * (1-P)).

    The rounding is per order, not per contract. Applying the cent ceiling to a
    single contract overstates the cost by up to sevenfold at the prices where
    favourites trade - $0.01 against a true $0.0014 at 98c - which is enough to
    bury a real edge under an imaginary cost.

    Sanity check against a live fill: a $10 stake at 74c buys 13.51 contracts and
    is charged 0.07 * 13.51 * 0.74 * 0.26 = $0.18.

    Reported for information only. No gate in this module uses it.
    """
    # Round before the ceiling. 0.07*12.5*0.8*0.2*100 evaluates to
    # 14.000000000000002 in binary floating point, and a bare ceil() would
    # charge 15 cents for a fee that is exactly 14.
    cents = round(FEE_RATE * contracts * price * (1 - price) * 100, 9)
    return math.ceil(cents) / 100


def kalshi_fee_rate(price: float) -> float:
    """Per-contract fee at realistic order size, before the per-order rounding.

    WARNING: this ignores the per-order rounding, which is exactly what makes
    small orders at extreme prices free. Prefer `kalshi_fee(price, contracts)`
    whenever the order size is known. Kept for aggregate estimates over many
    trades, where the rounding averages out.
    """
    return FEE_RATE * price * (1 - price)


def kalshi_fee_observed(price: float, contracts: float = 1) -> float:
    """Fee as Kalshi's own order ticket reports it.

    A ticket for 1 contract at 90c shows "Cost $0.90 ($0 fee)", while the
    published ceil() formula predicts a cent. The raw amount there is 0.63
    cents, so the exchange is evidently not rounding a sub-cent fee up to one.
    Flooring reproduces the ticket; the published ceil does not.

    This matters far more than it sounds. The strategy trades at 85-99c, where
    p(1-p) collapses - 0.07*0.95*0.05 is a third of a cent per contract - so at
    the one-contract size actually being traded the fee rounds away entirely.
    Charging a phantom cent there removed 23% of the measured edge.

    At larger sizes the fee is real and this agrees with the published formula:
    10 contracts at 75c is 13.1 cents, floor 13c, and a $10 stake at 74c was
    charged 18c in practice.

    Superseded for live accounting by `kalshi_fee_charged`: the ticket rounds to
    cents for display, but the account is debited to four decimal places. Kept
    because backtests scored with it are comparable only to each other.
    """
    return math.floor(round(FEE_RATE * contracts * price * (1 - price) * 100, 9)) / 100


def kalshi_fee_charged(price: float, contracts: float = 1) -> float:
    """Fee as actually debited, to the hundredth of a cent.

    Measured against real fills on 2026-09-20: 1 contract at 84c has a raw fee
    of 0.9408 cents and was charged $0.0095; a $10 stake at 74c has a raw fee of
    18.1818 cents and its ticket displayed $0.18. Both are explained by ceiling
    at four decimal places, with the ticket merely rounding that for display.

    The distinction is not pedantic at the size this system trades. At 1
    contract, `kalshi_fee_observed` floors to zero while the real charge is
    about a cent - and a cent is roughly a tenth of the expected profit on a
    $1 trade, so treating it as free would quietly overstate every result.
    """
    return math.ceil(round(FEE_RATE * contracts * price * (1 - price) * 10_000, 6)) / 10_000


def as_market_snapshot(snap: Snapshot) -> MarketSnapshot:
    return MarketSnapshot(
        price=snap.price,
        target=snap.target,
        bid_imbalance=0.0,
        taker_imbalance=snap.taker_imbalance,
        momentum_5m_bps=snap.momentum_5m_bps,
        volatility_5m_bps=snap.volatility_5m_bps,
        futures_basis_bps=0.0,
        spread_bps=0.0,
        window_high=snap.window_high,
        window_low=snap.window_low,
        elapsed_minutes=snap.elapsed,
    )


def _trade(
    snap: Snapshot, side: str, entry: float, pnl: float | None = None, took_profit: bool = False
) -> Trade | None:
    won = snap.won(side)
    if won is None:
        return None
    return Trade(
        ticker=snap.ticker,
        open_ms=snap.open_ms,
        side=side,
        entry_minute=snap.elapsed,
        entry_price=entry,
        won=won,
        pnl=pnl,
        took_profit=took_profit,
        session=snap.session,
        vol_regime=snap.vol_regime,
        trend_regime=snap.trend_regime,
        liquidity_regime=snap.liquidity_regime,
        distance_regime=snap.distance_regime,
    )


def primary_trades(
    snapshots: list[Snapshot],
    rule: EntryRule,
    candles: dict | None = None,
    closes: dict | None = None,
) -> list[Trade]:
    """Non-return strategy: back the side BTC is already on, near the deadline.

    This one genuinely holds to settlement, so it takes no take-profit; the
    candles/closes arguments exist only to match the reversion signature.
    """
    trades = []
    for snap in snapshots:
        if snap.remaining != rule.remaining_minutes:
            continue
        prediction = predict(as_market_snapshot(snap))
        entry = snap.entry_price(prediction.side)
        if entry is None or not 0 < entry < 1:
            continue
        matched, _ = rule.matches(prediction, as_market_snapshot(snap), entry)
        if not matched:
            continue
        trade = _trade(snap, prediction.side, entry)
        if trade:
            trades.append(trade)
    return trades


def reversion_trades(
    snapshots: list[Snapshot],
    rule: ReversionRule,
    candles: dict | None = None,
    closes: dict | None = None,
) -> list[Trade]:
    """Spike-reversion: fade a thrust through the strike, one entry per market.

    Scored on the rule's own take-profit. Without contract candles the exit
    cannot be checked, so the trade falls back to settlement - which understates
    a strategy whose whole design is to sell before expiry.
    """
    candles = candles or {}
    closes = closes or {}
    trades = []
    seen: set[str] = set()
    for snap in sorted(snapshots, key=lambda item: (item.open_ms, item.elapsed)):
        if snap.ticker in seen:
            continue
        setup = rule.setup(as_market_snapshot(snap), snap.remaining)
        if setup is None:
            continue
        entry = snap.entry_price(setup.side)
        if entry is None or not rule.min_entry_price <= entry <= rule.max_entry_price:
            continue
        best_exit = max_exit_after(
            candles.get(snap.ticker, []),
            snap.open_ms,
            snap.elapsed,
            setup.side,
            closes.get(snap.ticker),
        )
        took = best_exit >= rule.take_profit_price
        won = snap.won(setup.side)
        if won is None:
            continue
        payoff = rule.take_profit_price if took else (1.0 if won else 0.0)
        trade = _trade(snap, setup.side, entry, pnl=payoff - entry, took_profit=took)
        if trade:
            seen.add(snap.ticker)
            trades.append(trade)
    return trades


STRATEGIES = {"primary": primary_trades, "reversion": reversion_trades}


# -------------------------------------------------------------------- metrics


@dataclass(frozen=True)
class Result:
    trades: int
    wins: int
    win_rate: float
    average_entry: float
    total_cost: float
    gross_pnl: float
    gross_roi: float
    edge_per_contract: float
    edge_t_stat: float
    edge_ci_low: float
    edge_ci_high: float
    roi_ci_low: float
    roi_ci_high: float
    wilson_lower: float
    max_drawdown: float
    fee_pnl: float
    fee_roi: float

    @property
    def positive_gross_edge(self) -> bool:
        """Gross edge is positive with 95% confidence after block bootstrap."""
        return self.trades > 0 and self.edge_ci_low > 0


EMPTY = Result(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


def wilson_lower(wins: int, samples: int, z: float = 1.96) -> float:
    if samples == 0:
        return 0.0
    p = wins / samples
    denominator = 1 + z * z / samples
    centre = p + z * z / (2 * samples)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * samples)) / samples)
    return (centre - margin) / denominator


def block_bootstrap(
    values: list[float], costs: list[float], samples: int, seed: int, block: int = BLOCK_SIZE
) -> tuple[tuple[float, float], tuple[float, float]]:
    """95% CIs for mean edge and for ROI, using a moving-block bootstrap.

    Consecutive 15-minute BTC windows are not independent; resampling single
    trades would understate the spread. Resampling contiguous blocks keeps the
    local dependence structure intact.
    """
    count = len(values)
    if count < 2 or samples < 40:
        # Callers pass samples=0 in hot inner loops (grid search, permutations)
        # where only the point estimate is needed. Report no interval rather
        # than a meaningless one computed from a handful of resamples.
        return (0.0, 0.0), (0.0, 0.0)
    rng = random.Random(seed)
    span = min(block, count)
    blocks = max(1, math.ceil(count / span))
    means, rois = [], []
    for _ in range(samples):
        picked_values, picked_costs = [], []
        for _ in range(blocks):
            start = rng.randrange(count)
            for offset in range(span):
                index = (start + offset) % count
                picked_values.append(values[index])
                picked_costs.append(costs[index])
        total_cost = sum(picked_costs)
        means.append(sum(picked_values) / len(picked_values))
        rois.append(sum(picked_values) / total_cost if total_cost else 0.0)
    means.sort()
    rois.sort()
    low, high = int(0.025 * samples), int(0.975 * samples) - 1
    return (means[low], means[high]), (rois[low], rois[high])


def block_bootstrap_p(
    values: list[float], samples: int, seed: int, block: int = BLOCK_SIZE
) -> float:
    """Two-sided p-value from the same resampling that builds the interval.

    A t-statistic assumes independent observations; the interval next to it in
    the same table does not. Reporting one beside the other, and then running a
    false-discovery correction on the t-statistic, corrects the wrong number.
    """
    count = len(values)
    if count < 2 or samples < 40:
        return 1.0
    rng = random.Random(seed)
    span = min(block, count)
    blocks = max(1, math.ceil(count / span))
    below = ties = 0
    for _ in range(samples):
        total = 0.0
        drawn = 0
        for _ in range(blocks):
            start = rng.randrange(count)
            for offset in range(span):
                total += values[(start + offset) % count]
                drawn += 1
        mean = total / drawn
        if mean < 0:
            below += 1
        elif mean == 0:
            ties += 1
    # Ties count half to each side. Lumping them in with "below" turns a
    # perfectly balanced sample - every resample exactly zero - into p = 0.
    share = (below + 0.5 * ties) / samples
    return max(1.0 / samples, 2 * min(share, 1 - share))


def cluster_bootstrap(
    groups: dict, samples: int, seed: int
) -> tuple[tuple[float, float], float]:
    """95% CI and one-sided p for mean P&L, resampling whole MARKETS.

    A market contributes a separate row at every decision minute, and all of
    those rows share one settlement. Resampling rows - or even blocks of rows -
    treats ~4 copies of one coin flip as 4 flips and shrinks the interval by
    about 1.85x. Resampling markets keeps each outcome whole.
    """
    keys = list(groups)
    if len(keys) < 2 or samples < 40:
        return (0.0, 0.0), 1.0
    rng = random.Random(seed)
    count = len(keys)
    means = []
    for _ in range(samples):
        total = 0.0
        drawn = 0
        for _ in range(count):
            values = groups[keys[rng.randrange(count)]]
            total += sum(values)
            drawn += len(values)
        means.append(total / drawn if drawn else 0.0)
    means.sort()
    low = means[int(0.025 * samples)]
    high = means[int(0.975 * samples) - 1]
    below = sum(1 for value in means if value <= 0)
    return (low, high), max(1.0 / samples, below / samples)


def evaluate(
    trades: list[Trade], fee: float = 0.0, seed: int = 7, bootstrap: int = BOOTSTRAP_SAMPLES
) -> Result:
    """Gross-first metrics. `fee` affects only the reported fee_* fields."""
    if not trades:
        return EMPTY
    ordered = sorted(trades, key=lambda item: (item.open_ms, item.entry_minute))
    edges = [item.pnl for item in ordered]
    costs = [item.entry_price for item in ordered]
    count = len(ordered)
    wins = sum(item.won for item in ordered)
    total_cost = sum(costs)
    gross = sum(edges)
    mean_edge = gross / count
    deviation = (sum((value - mean_edge) ** 2 for value in edges) / max(count - 1, 1)) ** 0.5
    t_stat = mean_edge / (deviation / math.sqrt(count)) if deviation else 0.0
    (edge_low, edge_high), (roi_low, roi_high) = block_bootstrap(edges, costs, bootstrap, seed)

    equity = peak = drawdown = 0.0
    for value in edges:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)

    # Trading fees are charged per execution. Entry always pays; settlement is
    # free, but an early exit is a second execution and pays again.
    if fee < 0:
        fees = sum(
            kalshi_fee_rate(item.entry_price) * (2 if item.took_profit else 1)
            for item in ordered
        )
    else:
        fees = sum(fee * (2 if item.took_profit else 1) for item in ordered)
    return Result(
        trades=count,
        wins=wins,
        win_rate=wins / count,
        average_entry=total_cost / count,
        total_cost=round(total_cost, 4),
        gross_pnl=round(gross, 4),
        gross_roi=gross / total_cost if total_cost else 0.0,
        edge_per_contract=mean_edge,
        edge_t_stat=t_stat,
        edge_ci_low=edge_low,
        edge_ci_high=edge_high,
        roi_ci_low=roi_low,
        roi_ci_high=roi_high,
        wilson_lower=wilson_lower(wins, count),
        max_drawdown=round(drawdown, 4),
        fee_pnl=round(gross - fees, 4),
        fee_roi=(gross - fees) / total_cost if total_cost else 0.0,
    )


# ------------------------------------------------------------ rule selection


def candidates_to_trades(
    candidates: list[Candidate], indices: list[int], take_profit: float | None = None
) -> list[Trade]:
    return [
        Trade(
            ticker=item.ticker,
            open_ms=item.open_ms,
            side=item.side,
            entry_minute=item.elapsed,
            entry_price=item.entry_price,
            won=item.won,
            pnl=item.pnl(take_profit),
            took_profit=item.took_profit(take_profit),
            session=item.session,
            vol_regime=item.vol_regime,
            trend_regime=item.trend_regime,
            liquidity_regime=item.liquidity_regime,
            distance_regime=item.distance_regime,
        )
        for item in (candidates[index] for index in indices)
    ]


def mean_pnl(
    candidates: list[Candidate], indices: list[int], take_profit: float | None = None
) -> float:
    if not indices:
        return 0.0
    return sum(candidates[index].pnl(take_profit) for index in indices) / len(indices)


@dataclass(frozen=True)
class Selection:
    position: int
    rule: object
    take_profit: float | None
    trades: int
    mean_pnl: float


def select_rule(
    index: GridIndex, low: int, high: int, min_trades: int, robust: bool = True
) -> Selection | None:
    """Best rule over candidates[low:high], ranked by a lower confidence bound.

    Mean per contract rather than total P&L, because total rewards a rule simply
    for trading more often. And a *lower bound* rather than the mean, because
    ranking thousands of rules by their point estimate reliably crowns whichever
    small sample got luckiest: a rule with 60 trades and a fluke +$0.05 beats one
    with 4,000 trades and a durable +$0.01 every time. Penalising by standard
    error is what makes a broad, modest, real edge visible to the selector at
    all. Set robust=False to reproduce the naive point-estimate ranking.
    """
    best: Selection | None = None
    best_score = None
    for position in range(len(index)):
        count, value, _ = index.window_stats(position, low, high)
        if count < min_trades:
            continue
        score = index.window_lower_bound(position, low, high) if robust else value
        if best_score is None or score > best_score:
            best_score = score
            best = Selection(
                position=position,
                rule=index.rules[position],
                take_profit=index.take_profits[position],
                trades=count,
                mean_pnl=value,
            )
    return best


# --------------------------------------------------------------- walk-forward


@dataclass(frozen=True)
class Fold:
    index: int
    train_end_ms: int
    test_end_ms: int
    rule: dict | None
    train_trades: int
    train_edge: float
    test: Result


def walk_forward(
    index: GridIndex,
    folds: int = 6,
    min_train_trades: int = 80,
    seed: int = 7,
) -> tuple[list[Fold], list[Trade]]:
    """Refit on everything before each fold, trade the fold, never look back.

    The concatenated fold trades are the honest out-of-sample record of the
    whole procedure - rule search included - rather than of one lucky rule that
    was picked with hindsight over the full sample.
    """
    candidates = index.candidates
    if not candidates:
        return [], []
    start, end = index.open_times[0], index.open_times[-1] + 1
    span = (end - start) / (folds + 1)  # the first block is train-only

    records: list[Fold] = []
    out_of_sample: list[Trade] = []
    for number in range(1, folds + 1):
        train_end = int(start + span * number)
        test_end = int(start + span * (number + 1))
        train_low, train_high = index.slice_bounds(start, train_end)
        test_low, test_high = index.slice_bounds(train_end, test_end)

        chosen = select_rule(index, train_low, train_high, min_train_trades)
        if chosen is None:
            records.append(Fold(number, train_end, test_end, None, 0, 0.0, EMPTY))
            continue

        fold_trades = candidates_to_trades(
            candidates,
            index.rule_trades(chosen.position, test_low, test_high),
            chosen.take_profit,
        )
        out_of_sample.extend(fold_trades)
        records.append(
            Fold(
                index=number,
                train_end_ms=train_end,
                test_end_ms=test_end,
                rule=asdict(chosen.rule),
                train_trades=chosen.trades,
                train_edge=round(chosen.mean_pnl, 5),
                test=evaluate(fold_trades, seed=seed, bootstrap=400),
            )
        )
    return records, out_of_sample


# ----------------------------------------------------------- permutation test


def permutation_test(
    index: GridIndex,
    observed_edge: float,
    min_trades: int,
    permutations: int = PERMUTATIONS,
    seed: int = 7,
) -> dict:
    """p-value for the best-of-grid edge, correcting for the whole search.

    Each candidate's *outcome bundle* - the realised price path summary and the
    settlement result together - is shuffled within narrow entry-price buckets.
    Moving them as a pair keeps every bundle internally consistent, so a trade
    that reached 98c never gets paired with a settlement of zero. Shuffling
    within price buckets preserves the market's own price/probability
    calibration and destroys only the association between the strategy's
    features and what happened next, which is exactly the edge being claimed.

    The best mean P&L the entire grid can find on each shuffled sample forms the
    null, so a rule has to beat not just chance but the best of several thousand
    chances.
    """
    if observed_edge <= 0 or not len(index) or permutations < 1:
        return {"permutations": 0, "p_value": None, "note": "no positive observed edge"}

    rng = random.Random(seed)
    buckets: dict[int, list[int]] = {}
    for position, item in enumerate(index.candidates):
        buckets.setdefault(int(item.entry_price / PRICE_BUCKET), []).append(position)

    max_exits, wons = index.realised()
    null_edges = []
    for _ in range(permutations):
        shuffled_exits = list(max_exits)
        shuffled_wons = list(wons)
        for positions in buckets.values():
            bundles = [(max_exits[position], wons[position]) for position in positions]
            rng.shuffle(bundles)
            for position, (exit_price, won) in zip(positions, bundles, strict=True):
                shuffled_exits[position] = exit_price
                shuffled_wons[position] = won
        masks = index.outcome_masks(shuffled_exits, shuffled_wons)
        null_edges.append(index.best_mean_pnl_under(masks, min_trades))

    exceed = sum(1 for value in null_edges if value >= observed_edge)
    null_edges.sort()
    return {
        "permutations": permutations,
        "rules_searched": len(index),
        "observed_edge": round(observed_edge, 5),
        "p_value": (exceed + 1) / (permutations + 1),
        "null_median_edge": round(null_edges[len(null_edges) // 2], 5),
        "null_95th_edge": round(null_edges[int(0.95 * len(null_edges))], 5),
        "note": (
            "Outcome bundles (price path and settlement together) shuffled within 5-cent "
            "entry-price buckets; the best mean P&L the full grid finds on shuffled data "
            "is the null."
        ),
    }


# ------------------------------------------------------------------ baselines


def baseline_results(
    snapshots: list[Snapshot], seed: int = 7, minutes: tuple[int, ...] = ()
) -> dict[str, Result]:
    """What indiscriminate trading earns on the same markets.

    If the market is well calibrated these all sit at roughly zero gross edge.
    A strategy that does not clear them is not a strategy.

    Spans every decision minute the strategies can trade. Measuring the
    benchmark at one minute while the strategies trade ten compares them to a
    bar drawn somewhere else.
    """
    allowed = set(minutes) if minutes else None
    rng = random.Random(seed)
    always_yes, always_favourite, coin_flip = [], [], []
    for snap in snapshots:
        if snap.yes_ask is None or snap.yes_bid is None:
            continue
        if allowed is not None and snap.remaining not in allowed:
            continue
        yes_entry = snap.entry_price("UP")
        no_entry = snap.entry_price("DOWN")
        if yes_entry is None or no_entry is None:
            continue
        if 0 < yes_entry < 1:
            trade = _trade(snap, "UP", yes_entry)
            if trade:
                always_yes.append(trade)
        # The favourite is the side the market prices as MORE likely, i.e. the
        # dearer contract. Taking the cheaper one backs the underdog instead.
        favourite = "UP" if yes_entry >= no_entry else "DOWN"
        entry = yes_entry if favourite == "UP" else no_entry
        if 0 < entry < 1:
            trade = _trade(snap, favourite, entry)
            if trade:
                always_favourite.append(trade)
        side = "UP" if rng.random() < 0.5 else "DOWN"
        entry = yes_entry if side == "UP" else no_entry
        if 0 < entry < 1:
            trade = _trade(snap, side, entry)
            if trade:
                coin_flip.append(trade)
    return {
        "always_yes": evaluate(always_yes, seed=seed, bootstrap=500),
        "always_favourite": evaluate(always_favourite, seed=seed, bootstrap=500),
        "coin_flip": evaluate(coin_flip, seed=seed, bootstrap=500),
    }


# ------------------------------------------------------------------- regimes


def benjamini_hochberg(p_values: list[float], alpha: float = 0.10) -> list[bool]:
    """Which p-values survive a 10% false-discovery-rate correction."""
    indexed = sorted(enumerate(p_values), key=lambda pair: pair[1])
    count = len(p_values)
    keep = [False] * count
    largest = -1
    for rank, (_, value) in enumerate(indexed, start=1):
        if value <= alpha * rank / count:
            largest = rank
    for rank, (position, _) in enumerate(indexed, start=1):
        if rank <= largest:
            keep[position] = True
    return keep


def _normal_p(t_stat: float) -> float:
    """Two-sided p-value from a t statistic, normal approximation."""
    return math.erfc(abs(t_stat) / math.sqrt(2))


def regime_breakdown(trades: list[Trade], dimensions: tuple[str, ...] = ()) -> list[dict]:
    """Per-regime gross edge, FDR-corrected across all buckets tested.

    Slicing a sample many ways always produces a best slice. The correction is
    what separates a regime finding from the shape of the noise.
    """
    dimensions = dimensions or (
        "session",
        "vol_regime",
        "trend_regime",
        "liquidity_regime",
        "distance_regime",
        "side",
    )
    rows = []
    for dimension in dimensions:
        buckets: dict[str, list[Trade]] = {}
        for trade in trades:
            buckets.setdefault(getattr(trade, dimension), []).append(trade)
        for bucket, subset in sorted(buckets.items()):
            if len(subset) < 20:
                continue
            result = evaluate(subset, bootstrap=600)
            rows.append(
                {
                    "dimension": dimension,
                    "bucket": bucket,
                    "trades": result.trades,
                    "win_rate": round(result.win_rate, 4),
                    "roi": round(result.gross_roi, 4),
                    "edge_per_contract": round(result.edge_per_contract, 4),
                    "lower_95": round(result.edge_ci_low, 4),
                    "upper_95": round(result.edge_ci_high, 4),
                    "p_value": round(
                        block_bootstrap_p([item.pnl for item in subset], 600, seed=23), 5
                    ),
                }
            )
    if rows:
        keep = benjamini_hochberg([row["p_value"] for row in rows])
        for row, survives in zip(rows, keep, strict=True):
            row["verdict"] = (
                ("favourable" if row["edge_per_contract"] > 0 else "adverse")
                if survives
                else "noise"
            )
    return rows


# ------------------------------------------------- hypothesis-driven rules


HYPOTHESIS_RULES = {
    "favourite_10min": EntryRule(remaining_minutes=10, min_ask=0.85, max_ask=0.99),
    "favourite_9min": EntryRule(remaining_minutes=9, min_ask=0.85, max_ask=0.99),
    "favourite_7min": EntryRule(remaining_minutes=7, min_ask=0.85, max_ask=0.99),
    "favourite_5min": EntryRule(remaining_minutes=5, min_ask=0.85, max_ask=0.99),
    "deep_favourite_10min": EntryRule(remaining_minutes=10, min_ask=0.90, max_ask=0.99),
    "confident_far_10min": EntryRule(
        remaining_minutes=10,
        min_raw_probability=0.80,
        min_normalized_distance=2.0,
        min_ask=0.30,
        max_ask=0.99,
    ),
    "longshot_10min": EntryRule(remaining_minutes=10, min_ask=0.05, max_ask=0.30),
}


def hypothesis_results(index: GridIndex, seed: int = 7) -> list[dict]:
    """Evaluate a handful of named rules with no search behind them.

    A grid of thousands needs a permutation test because the *search* can
    manufacture an edge. Seven rules fixed in advance need only an honest
    interval and a correction across those seven. Each is also split in half
    chronologically: an effect present in one half and absent in the other is a
    regime, not an edge.

    Honesty note: these were written after looking at the full-sample
    calibration curve, so the halves are the real evidence, not the total.
    """
    candidates = index.candidates
    if not candidates:
        return []
    midpoint = candidates[len(candidates) // 2].open_ms
    split = index.slice_bounds(0, midpoint)[1]

    rows = []
    for name, rule in HYPOTHESIS_RULES.items():
        matched = MATCHERS["primary"](candidates, rule)
        if len(matched) < 30:
            continue
        whole = evaluate(candidates_to_trades(candidates, matched), seed=seed, bootstrap=1000)
        first = evaluate(
            candidates_to_trades(candidates, [i for i in matched if i < split]),
            seed=seed,
            bootstrap=600,
        )
        second = evaluate(
            candidates_to_trades(candidates, [i for i in matched if i >= split]),
            seed=seed,
            bootstrap=600,
        )
        rows.append(
            {
                "name": name,
                "rule": asdict(rule),
                "trades": whole.trades,
                "win_rate": round(whole.win_rate, 4),
                "average_entry": round(whole.average_entry, 4),
                "edge_per_contract": round(whole.edge_per_contract, 5),
                "edge_ci_low": round(whole.edge_ci_low, 5),
                "edge_ci_high": round(whole.edge_ci_high, 5),
                "gross_roi": round(whole.gross_roi, 5),
                "fee_roi": round(whole.fee_roi, 5),
                "p_value": round(
                    block_bootstrap_p(
                        [item.pnl for item in candidates_to_trades(candidates, matched)],
                        1000,
                        seed=29,
                    ),
                    5,
                ),
                "first_half_trades": first.trades,
                "first_half_edge": round(first.edge_per_contract, 5),
                "second_half_trades": second.trades,
                "second_half_edge": round(second.edge_per_contract, 5),
                "both_halves_positive": bool(
                    first.edge_per_contract > 0 and second.edge_per_contract > 0
                ),
            }
        )
    if rows:
        keep = benjamini_hochberg([row["p_value"] for row in rows], alpha=0.10)
        for row, survives in zip(rows, keep, strict=True):
            row["verdict"] = (
                "noise"
                if not survives
                else ("favourable" if row["edge_per_contract"] > 0 else "adverse")
            )
    return rows


# ------------------------------------------------------------------- sizing


def position_size(price: float, contracts: float | None, cash: float | None) -> tuple[float, float]:
    """(contracts, cost) for one order.

    Two conventions, and they are not interchangeable in dollars even though
    their ROI is identical:

    * `contracts=10` at 64c buys 10 contracts for $6.40; a loss costs $6.40.
    * `cash=10` at 64c buys 15.6 contracts for $10.00; a loss costs $10.00.

    Reporting one while the bot places the other misstates the money at risk,
    which is why the caller must say which it means.
    """
    if contracts is not None:
        return contracts, contracts * price
    if cash is not None:
        return (cash / price if price else 0.0), cash
    raise ValueError("specify contracts= or cash=")


def trade_pnl(
    price: float,
    won: bool,
    *,
    contracts: float | None = None,
    cash: float | None = None,
    exited: bool = False,
) -> float:
    """Net dollars for one settled position, after Kalshi's per-order fee.

    Uses `kalshi_fee_charged` - what the account is actually debited - rather
    than the cent-floored ticket display. At one contract, the size this bot
    orders, flooring makes the fee exactly zero while the exchange charged
    $0.0095, so a message reading "after fees" had charged none. It also put
    this figure about a cent per trade out of step with the daily loss floor,
    which already used the charged model.
    """
    count, cost = position_size(price, contracts, cash)
    payout = count if won else 0.0
    fees = kalshi_fee_charged(price, count) * (2 if exited else 1)
    return payout - cost - fees


def contracts_for_budget(budget: float, price: float, minimum: int = 1) -> int:
    """Whole contracts buyable for `budget` dollars of cost at `price`.

    Kalshi trades whole contracts, so a dollar budget has to round. It rounds
    DOWN to avoid quietly spending more than asked, with a floor of one contract
    - a budget smaller than one contract's price buys one, because the
    alternative is an order that cannot exist.
    """
    if price <= 0:
        return minimum
    return max(minimum, int(budget / price))
