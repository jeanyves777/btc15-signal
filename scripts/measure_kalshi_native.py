"""Can the entry rule run on Kalshi data alone, with no price feed at all?

THE OPERATOR'S DIRECTIVE: Kalshi becomes the source of truth; Binance must
never decide which side is winning, how far the official target is, or whether
a signal passes. The obvious reading is "swap Binance for BRTI" - but Kalshi
publishes BRTI only at the two window boundaries (`floor_strike` at open,
`expiration_value` after close) and exposes no index passthrough, authenticated
or not. There is no intra-window BRTI to swap in.

So the directive is met a different way: **drop the underlying price from the
entry rule entirely.** The contract ask already IS the market's estimate of
distance-to-strike, expressed as a probability, and sections 36 and 37 measured
that the price beats every model built on the Binance path anyway. What is
tested here:

    A  deployed          side from Binance distance, Binance distance gate
    B  kalshi-native     side = whichever side's ask is in band; band only
    C  kalshi-native     + implied-distance gate, z = Phi^-1(ask), the
                           market's own normalized distance in Kalshi units
    D  kalshi-native     + BRTI realised volatility from the boundary chain

B, C and D read NO Binance data of any kind - not klines, not the snapshot.
The only prices they touch are Kalshi quotes and Kalshi's own BRTI boundary
values, which is exactly what the directive asks for.

The question is not whether Kalshi-native is philosophically better. It is
whether it costs anything, measured net of fees on the same corpus.

    python scripts/measure_kalshi_native.py
"""

import argparse
import math
import random
import sqlite3
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

DB = "data/market_data.db"


# ------------------------------------------------------------------ helpers

def net(price: float, won: bool) -> float:
    return (1.0 if won else 0.0) - price - fee(price)


def paired_ci(values: list[float], draws: int = 4000, seed: int = 11):
    if len(values) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def inverse_normal(p: float) -> float:
    """Phi^-1 via the standard rational approximation.

    This is the market's own normalized distance: a contract at 0.84 is saying
    the strike sits about one standard deviation away over the time remaining.
    It replaces `normalized_distance` without reading a price feed.
    """
    p = min(max(p, 1e-6), 1 - 1e-6)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ------------------------------------------------------- the BRTI price chain

def brti_chain(db: sqlite3.Connection) -> list[dict]:
    """Kalshi's own BRTI values, one per window boundary.

    `floor_strike[N] == expiration_value[N-1]` exactly (FINDINGS 41), so the
    strikes ARE a BRTI price series sampled every 15 minutes. It is real BRTI,
    not a proxy, and it is the only underlying price this script uses.
    """
    return [
        dict(row) for row in db.execute(
            "SELECT ticker, open_ms, close_ms, floor_strike, expiration_value, result "
            "FROM markets WHERE result IN ('yes','no') AND floor_strike IS NOT NULL "
            "AND expiration_value IS NOT NULL ORDER BY open_ms"
        )
    ]


def brti_volatility(chain: list[dict], lookback: int = 16) -> dict[str, float]:
    """Realised BRTI volatility in bps, from the trailing boundary values.

    Uses only windows that have ALREADY settled, so nothing here can see its
    own outcome. 16 windows is four hours.
    """
    out: dict[str, float] = {}
    returns: list[float] = []
    for index, market in enumerate(chain):
        if index and chain[index - 1]["floor_strike"]:
            previous = chain[index - 1]["floor_strike"]
            returns.append((market["floor_strike"] / previous - 1) * 10_000)
        if len(returns) >= lookback:
            out[market["ticker"]] = st.pstdev(returns[-lookback:])
    return out


# --------------------------------------------------------------- trade builds

def load_quotes(db: sqlite3.Connection) -> dict[str, list[tuple[int, float, float]]]:
    quotes: dict[str, list[tuple[int, float, float]]] = {}
    for row in db.execute(
        "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
        "FROM contract_candles WHERE yes_bid_close IS NOT NULL "
        "AND yes_ask_close IS NOT NULL ORDER BY ticker, end_period_ts"
    ):
        quotes.setdefault(row["ticker"], []).append(
            (row["end_period_ts"], row["yes_bid_close"], row["yes_ask_close"])
        )
    return quotes


def kalshi_native_trades(
    rule: EntryRule, settings: Settings, chain: list[dict],
    quotes: dict, vol: dict[str, float],
) -> list[dict]:
    """First qualifying minute per market, Kalshi data only.

    SIDE SELECTION is the part that changes. The live rule reads a Binance
    price and asks which side of the strike it sits on. Here the side is simply
    whichever contract is in the band - which, in a 0.70-0.93 band, is the
    dearer one, since the two asks sum to 1 plus the spread. That is the
    convention FINDINGS 13 measured at +0.0248 against the live rule's +0.0220.
    """
    from_s, to_s = settings.entry_from_seconds, settings.entry_to_seconds
    trades = []
    for market in chain:
        rows = quotes.get(market["ticker"])
        if not rows:
            continue
        close_s = market["close_ms"] // 1000
        for end_ts, yes_bid, yes_ask in rows:
            remaining = close_s - end_ts
            if not to_s <= remaining <= from_s:
                continue
            up_ask, down_ask = yes_ask, round(1 - yes_bid, 4)
            candidates = [
                (side, ask) for side, ask in (("UP", up_ask), ("DOWN", down_ask))
                if rule.min_ask <= ask <= rule.max_ask and 0 < ask < 1
            ]
            if not candidates:
                continue
            side, ask = max(candidates, key=lambda item: item[1])
            won = (market["result"] == "yes") if side == "UP" else (market["result"] == "no")
            trades.append({
                "ticker": market["ticker"], "side": side, "ask": ask, "won": won,
                "implied_z": abs(inverse_normal(ask)),
                "brti_vol_bps": vol.get(market["ticker"]),
                "remaining_s": remaining,
            })
            break
    return trades


def deployed_trades(rule: EntryRule, settings: Settings) -> list[dict]:
    """The rule as it actually runs today, Binance distance gate and all."""
    from measure_close_call import build_trades  # noqa: PLC0415

    return [
        {"ticker": t["ticker"], "side": t["side"], "ask": t["entry"], "won": t["won"]}
        for t in build_trades(rule, settings)
    ]


# -------------------------------------------------------------------- report

def side_disagreement(rule: EntryRule, settings: Settings, native: list[dict]) -> None:
    """Where the two instruments pick DIFFERENT sides, which one is right?

    This is the question the directive turns on. The 19% figure in section 40
    is a SETTLEMENT-time disagreement, measured where margins are tenths of a
    basis point. Entry happens 6-11 minutes out behind a distance gate, so the
    disagreement rate there is a different number and has to be measured
    separately before it can justify anything.
    """
    from measure_close_call import build_trades  # noqa: PLC0415

    # PAIRED AT THE SAME MINUTE. Comparing each rule's own entry would confound
    # the side convention with entry timing - two rules that enter different
    # minutes are not a test of anything. Here the market, the minute and the
    # book are identical and ONLY the convention differs.
    quotes = {t["ticker"]: t for t in native}
    pairs, disagree = 0, []
    for trade in build_trades(rule, settings):
        quote = quotes.get(trade["ticker"])
        if quote is None or quote["remaining_s"] != trade["entry_remaining"] * 60:
            continue
        pairs += 1
        if quote["side"] != trade["side"]:
            disagree.append((trade, quote))

    print("\nSIDE SELECTION: Binance distance-sign vs Kalshi in-band side")
    print("  paired at the SAME market and the SAME minute, so only the "
          "convention differs")
    print(f"  markets compared              {pairs}")
    if not pairs:
        return
    print(f"  they pick a different side    {len(disagree)} "
          f"({len(disagree) / pairs:.2%})")
    if not disagree:
        return

    binance_right = sum(1 for t, _ in disagree if t["won"])
    kalshi_right = sum(1 for _, q in disagree if q["won"])
    binance_net = sum(net(t["entry"], t["won"]) for t, _ in disagree)
    kalshi_net = sum(net(q["ask"], q["won"]) for _, q in disagree)
    n = len(disagree)
    print(f"  ON THOSE {n} MARKETS:")
    print(f"    Binance side correct        {binance_right:>4} "
          f"({binance_right / n:.1%})   net {binance_net:+.2f}")
    print(f"    Kalshi side correct         {kalshi_right:>4} "
          f"({kalshi_right / n:.1%})   net {kalshi_net:+.2f}")
    print(f"    Kalshi minus Binance                          "
          f"net {kalshi_net - binance_net:+.2f} "
          f"({(kalshi_net - binance_net) / n:+.4f}/contract over the disagreements)")
    print(f"    spread over ALL {pairs} paired entries:       "
          f"{(kalshi_net - binance_net) / pairs:+.4f}/contract")


def summarise(name: str, trades: list[dict], note: str = "") -> dict:
    if not trades:
        print(f"  {name:<34} no trades")
        return {}
    values = [net(t["ask"], t["won"]) for t in trades]
    mean = sum(values) / len(values)
    low, high = paired_ci(values)
    wins = sum(1 for t in trades if t["won"])
    print(f"  {name:<34} n={len(trades):<5} {mean:+.4f}/ct "
          f"[{low:+.4f},{high:+.4f}]  total {sum(values):+8.2f}  "
          f"win {wins / len(trades):.1%}  {note}")
    return {"mean": mean, "n": len(trades), "total": sum(values)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--z-min", type=float, default=1.0)
    parser.add_argument("--vol-max", type=float, default=None,
                        help="skip windows whose trailing BRTI vol exceeds this (bps)")
    args = parser.parse_args()

    settings = Settings()
    rule = EntryRule.load(Path("strategy.json"))
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    chain = brti_chain(db)
    vol = brti_volatility(chain)
    quotes = load_quotes(db)
    print(f"corpus: {len(chain)} settled markets, {len(vol)} with trailing BRTI vol")
    print(f"band {rule.min_ask}-{rule.max_ask}, entry "
          f"{settings.entry_from_seconds}-{settings.entry_to_seconds}s\n")

    native = kalshi_native_trades(rule, settings, chain, quotes, vol)

    print("ENTRY RULE, net of fees")
    summarise("A  deployed (Binance-gated)", deployed_trades(rule, settings),
              "reads Binance")
    summarise("B  kalshi-native, band only", native, "reads NO price feed")
    summarise(
        f"C  kalshi-native + implied z>={args.z_min}",
        [t for t in native if t["implied_z"] >= args.z_min], "reads NO price feed",
    )
    if args.vol_max:
        summarise(
            f"D  C + BRTI vol <= {args.vol_max}",
            [t for t in native
             if t["implied_z"] >= args.z_min and (t["brti_vol_bps"] or 0) <= args.vol_max],
            "BRTI vol from Kalshi boundaries",
        )

    side_disagreement(rule, settings, native)

    print("\nIMPLIED DISTANCE z = Phi^-1(ask), the Kalshi-native replacement "
          "for normalized_distance")
    print(f"  {'z bucket':>12} {'n':>6} {'mean ask':>9} {'net/ct':>9} {'win':>7}")
    for low, high in ((0.5, 0.8), (0.8, 1.0), (1.0, 1.2), (1.2, 1.5), (1.5, 9.9)):
        bucket = [t for t in native if low <= t["implied_z"] < high]
        if len(bucket) < 30:
            continue
        values = [net(t["ask"], t["won"]) for t in bucket]
        print(f"  {low:.1f} - {high:.1f}  {len(bucket):>6} "
              f"{st.mean([t['ask'] for t in bucket]):>9.3f} "
              f"{sum(values) / len(values):>+9.4f} "
              f"{sum(1 for t in bucket if t['won']) / len(bucket):>7.1%}")


if __name__ == "__main__":
    main()
