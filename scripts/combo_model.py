"""A joint model for crypto 15-minute outcomes: one common factor, fitted.

WHY THE FLAT LIFT WAS WRONG. The first selector estimated a combo's true
probability as `product x lift(legs, minutes)`. Because that multiplier does not
depend on the PRICES, every combo of the same shape got the same edge - +690%
across the board - so the ranking was really just "cheapest first". It happily
recommended ALL DOWN on a basket containing BNB at 0.73-up and ETH at 0.72-up,
which the correlation argument does not support at all: the lift says assets move
TOGETHER, not that the market's direction call is wrong.

THE MODEL. One latent factor, the thing that actually drives a 15-minute crypto
window - the whole complex drifting up or down together:

    X_i = sqrt(rho) * Z + sqrt(1 - rho) * E_i        Z, E_i ~ N(0,1)
    asset i finishes UP  <=>  X_i > k_i
    k_i chosen so P(X_i > k_i) = p_i, the market's own quoted probability

Conditional on Z the legs are independent, so a joint probability is a
one-dimensional integral over Z - cheap and exact enough on a grid. This
reproduces two things a flat multiplier cannot:

  * the marginals are always respected, so a leg priced 0.73-up stays 0.73-up;
  * agreement is common when legs are near 50/50 and rare when the market has
    already split them, which is the behaviour the flat lift got backwards.

rho is FITTED to the historical record rather than assumed: it is chosen so the
model reproduces the observed frequency of all five assets settling the same way
(45.4% against a 19.3% product) on 40,578 aligned points.

No fees, per the operator's standing instruction.
"""

import math
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MARKETS = {
    "BTC": "market_data.db",
    "ETH": "market_data_kxeth15m.db",
    "SOL": "market_data_kxsol15m.db",
    "XRP": "market_data_kxxrp15m.db",
    "NEAR": "market_data_kxnear15m.db",
}

# Gauss-Legendre-ish grid over the latent factor. 161 points across +-5 sigma
# is far finer than the data can distinguish and costs nothing.
_GRID = [(-5.0 + 10.0 * i / 160.0) for i in range(161)]
_STEP = 10.0 / 160.0
_WEIGHT = [math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi) * _STEP for z in _GRID]


def _ndtr(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _ndtri(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation)."""
    p = min(max(p, 1e-9), 1 - 1e-9)
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
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def joint(probs, directions, rho: float) -> float:
    """P(every leg lands the way `directions` says), given quoted `probs`."""
    if not 0.0 <= rho < 1.0:
        raise ValueError("rho must be in [0, 1)")
    ks = [_ndtri(1.0 - p) for p in probs]
    root, rest = math.sqrt(rho), math.sqrt(1.0 - rho)
    total = 0.0
    for z, w in zip(_GRID, _WEIGHT):
        term = 1.0
        for k, up in zip(ks, directions):
            # P(X_i > k | Z=z) with X_i = root*z + rest*E_i
            p_up = 1.0 - _ndtr((k - root * z) / rest)
            term *= p_up if up else (1.0 - p_up)
            if term == 0.0:
                break
        total += w * term
    return total


def _outcomes():
    by = {}
    for asset, db in MARKETS.items():
        path = ROOT / "data" / db
        if not path.exists():
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for close_ms, result in con.execute(
                "SELECT close_ms, result FROM markets "
                " WHERE result IN ('yes','no') AND floor_strike IS NOT NULL"):
            by.setdefault(int(close_ms), {})[asset] = (result == "yes")
        con.close()
    assets = sorted(MARKETS)
    return assets, {w: v for w, v in by.items() if len(v) == len(assets)}


def fit_rho() -> float:
    """Choose rho so the model reproduces the observed all-agree frequency."""
    assets, full = _outcomes()
    n = len(full)
    marg = [sum(1 for v in full.values() if v[a]) / n for a in assets]
    same = sum(1 for v in full.values()
               if all(v[a] for a in assets) or not any(v[a] for a in assets)) / n
    lo, hi = 0.0, 0.97
    for _ in range(60):
        mid = (lo + hi) / 2
        model = (joint(marg, [True] * len(assets), mid)
                 + joint(marg, [False] * len(assets), mid))
        if model < same:
            lo = mid
        else:
            hi = mid
    rho = (lo + hi) / 2
    model = (joint(marg, [True] * len(assets), rho)
             + joint(marg, [False] * len(assets), rho))
    product = (math.prod(marg) + math.prod(1 - p for p in marg))
    print(f"  aligned windows      {n}")
    print(f"  marginals            " + ", ".join(
        f"{a} {p:.1%}" for a, p in zip(assets, marg)))
    print(f"  all five agree       observed {same:.1%}   "
          f"independence says {product:.1%}")
    print(f"  fitted rho           {rho:.4f}   model reproduces {model:.1%}")
    return rho


if __name__ == "__main__":
    print("FITTING THE COMMON FACTOR")
    rho = fit_rho()
    print(f"\n  a same-direction pair at 50/50 under this model:")
    print(f"    both up  {joint([0.5, 0.5], [True, True], rho):.1%} "
          f"(independence 25.0%)")
    print(f"  and a pair the market has already split:")
    print(f"    both up when priced 0.73 and 0.27: "
          f"{joint([0.73, 0.27], [True, True], rho):.1%} "
          f"(independence {0.73 * 0.27:.1%})")
