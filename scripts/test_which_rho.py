"""Whose correlation is right - ours (0.69) or the one Kalshi charges (~0.92)?

THE QUESTION AND WHY IT DECIDES REAL MONEY. Backing a one-factor copula out of
the app's combo prices gives an implied rho near 0.92 on 3-leg baskets and near
0.72 on 8-leg ones, against the 0.6944 fitted here from settlement history. Read
one way that says Kalshi overcharges small baskets. Read the other way it says
OUR rho is too low, and the "markup" is just our own model error showing up
wherever rho has the most leverage on the answer.

Prices cannot separate those. Outcomes can.

THE TEST. For every (window, minute) where all five assets were quoted at once,
take each asset's quoted probability, form every k-asset subset, and compute the
model's predicted probability that they ALL settle up at a range of rho. Compare
against what actually happened. The rho whose predictions match observed
frequency is the real one - and the marginals come from the market's own quotes,
not from us, so this is not a restatement of the fit.

Two things make it a fair test:

  * the original rho was fitted to the FIVE-asset all-agree frequency ONLY. This
    scores 2-, 3- and 4-asset subsets too, which that fit never saw.
  * it scores at REAL mid-window quoted prices, where a combo is actually
    bought, not at the ~50% marginals of a window's open.

Scored by mean absolute calibration error over subsets and by the total
log-likelihood of the observed all-up indicator. Log-likelihood is the one that
matters: it is a proper scoring rule, so it cannot be gamed by a model that gets
the average right while getting every individual case wrong.

IMPLEMENTATION NOTE. The first version of this called combo_model.joint() once
per (subset, key, rho) and would have taken hours. Conditional on the latent
factor the legs are independent, so for one key and one rho you can build each
asset's P(up | Z=z) ONCE across the 161-point grid and then every subset is a
product over its members. That is the whole speedup.

No fees, per the operator's standing instruction.
"""

import itertools
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

MARKETS = {
    "BTC": "market_data.db",
    "ETH": "market_data_kxeth15m.db",
    "SOL": "market_data_kxsol15m.db",
    "XRP": "market_data_kxxrp15m.db",
    "NEAR": "market_data_kxnear15m.db",
}
RHOS = [0.0, 0.50, 0.60, 0.6944, 0.75, 0.80, 0.85, 0.9176, 0.95]

# the same 161-point grid combo_model uses, so results stay comparable
GRID = np.linspace(-5.0, 5.0, 161)
STEP = 10.0 / 160.0
WEIGHT = np.exp(-0.5 * GRID ** 2) / math.sqrt(2 * math.pi) * STEP


def ndtr(x):
    from scipy.special import ndtr as _n  # noqa: F401
    return _n(x)


try:
    from scipy.special import ndtr as _ndtr  # type: ignore

    def norm_cdf(x):
        return _ndtr(x)
except Exception:  # scipy is not a dependency of the live service
    _SQRT2 = math.sqrt(2.0)
    _vec_erf = np.vectorize(math.erf)

    def norm_cdf(x):
        return 0.5 * (1.0 + _vec_erf(np.asarray(x) / _SQRT2))


def ndtri(p):
    """Inverse standard normal CDF, Acklam - vectorised over an array."""
    p = np.clip(np.asarray(p, dtype=float), 1e-9, 1 - 1e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    out = np.empty_like(p)
    lo = p < plow
    hi = p > phigh
    mid = ~(lo | hi)
    if lo.any():
        q = np.sqrt(-2 * np.log(p[lo]))
        out[lo] = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                  ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if hi.any():
        q = np.sqrt(-2 * np.log(1 - p[hi]))
        out[hi] = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                   ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if mid.any():
        q = p[mid] - 0.5
        r = q * q
        out[mid] = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
                   (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    return out


def quoted(market_db):
    """{(close_ms, minutes_left): (p_up, rose)} straight from the candle book."""
    mp = ROOT / "data" / market_db
    if not mp.exists():
        return {}
    con = sqlite3.connect(f"file:{mp}?mode=ro", uri=True)
    result = {}
    for ticker, close_ms, res in con.execute(
            "SELECT ticker, close_ms, result FROM markets "
            " WHERE result IN ('yes','no') AND floor_strike IS NOT NULL"):
        result[ticker] = (int(close_ms), res == "yes")
    out = {}
    for ticker, ts, bid, ask in con.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
        if bid is None or ask is None or ticker not in result:
            continue
        close_ms, rose = result[ticker]
        ms = ts * 1000 if ts < 1e11 else ts
        minutes = round((close_ms - ms) / 60000)
        if not 1 <= minutes <= 14:
            continue
        yb = bid / 100 if bid > 1 else bid
        ya = ask / 100 if ask > 1 else ask
        p = (yb + ya) / 2
        if not 0.02 <= p <= 0.98:
            continue
        out[(close_ms, minutes)] = (p, rose)
    con.close()
    return out


def main():
    print("=" * 78)
    print("WHICH RHO PREDICTS ACTUAL JOINT SETTLEMENTS?")
    print("=" * 78)
    data = {a: quoted(db) for a, db in MARKETS.items()}
    assets = [a for a in MARKETS if data.get(a)]
    for a in assets:
        print(f"  {a:<5} {len(data[a])} quoted decision points")
    keys = sorted(set.intersection(*(set(data[a]) for a in assets)))
    n_keys = len(keys)
    print(f"\n  {n_keys} (window, minute) points with all {len(assets)} "
          f"assets quoted at once")
    if n_keys < 300:
        print("  too few to decide")
        return

    # P[asset, key] and UP[asset, key]
    P = np.array([[data[a][k][0] for k in keys] for a in assets])
    UP = np.array([[data[a][k][1] for k in keys] for a in assets])
    K = ndtri(1.0 - P)                       # thresholds, (n_assets, n_keys)

    subsets = [c for k in range(2, len(assets) + 1)
               for c in itertools.combinations(range(len(assets)), k)]
    print(f"  {len(subsets)} subsets scored per rho, "
          f"{len(subsets) * n_keys:,} predictions each\n")

    mae = defaultdict(dict)     # rho -> k -> mean |pred - obs| weighted
    loglik = {}
    for rho in RHOS:
        root, rest = math.sqrt(rho), math.sqrt(1.0 - rho)
        if rest < 1e-12:
            rest = 1e-12
        # cond[a, key, z] = P(asset a up | Z = z)
        cond = 1.0 - norm_cdf(
            (K[:, :, None] - root * GRID[None, None, :]) / rest)
        tot_e = defaultdict(float)
        tot_n = defaultdict(int)
        ll = 0.0
        for sub in subsets:
            term = np.ones((n_keys, GRID.size))
            for a in sub:
                term *= cond[a]
            pred = term @ WEIGHT                     # (n_keys,)
            obs = np.all(UP[list(sub)], axis=0)
            q = np.clip(pred, 1e-9, 1 - 1e-9)
            ll += float(np.sum(np.where(obs, np.log(q), np.log(1 - q))))
            k = len(sub)
            tot_e[k] += abs(float(pred.mean() - obs.mean())) * n_keys
            tot_n[k] += n_keys
        for k in tot_n:
            mae[rho][k] = tot_e[k] / tot_n[k]
        loglik[rho] = ll

    ks = sorted(mae[RHOS[0]])
    print("  MEAN ABSOLUTE CALIBRATION ERROR, predicted vs observed all-up")
    print("  " + f"{'rho':>8}" + "".join(f"{('k=' + str(k)):>9}" for k in ks)
          + f"{'overall':>10}{'logLik':>13}")
    best_mae = best_ll = None
    for rho in RHOS:
        cells = "".join(f"{mae[rho][k]:>9.4f}" for k in ks)
        overall = sum(mae[rho][k] for k in ks) / len(ks)
        if best_mae is None or overall < best_mae[1]:
            best_mae = (rho, overall)
        if best_ll is None or loglik[rho] > best_ll[1]:
            best_ll = (rho, loglik[rho])
        mark = ""
        if abs(rho - 0.6944) < 1e-9:
            mark = "  <- ours"
        elif abs(rho - 0.9176) < 1e-9:
            mark = "  <- Kalshi 3-leg"
        print(f"  {rho:>8.4f}{cells}{overall:>10.4f}{loglik[rho]:>13.0f}{mark}")

    print(f"\n  BEST BY CALIBRATION:     rho = {best_mae[0]:.4f} "
          f"(MAE {best_mae[1]:.4f})")
    print(f"  BEST BY LOG-LIKELIHOOD:  rho = {best_ll[0]:.4f}   "
          f"<- the proper scoring rule, trust this one")
    print()
    if best_ll[0] <= 0.75:
        print("  VERDICT: our rho is supported. The implied 0.92 on 3-leg app")
        print("  prices is a genuine markup, not our model error.")
    elif best_ll[0] >= 0.85:
        print("  VERDICT: our rho was too low. Kalshi has been pricing these")
        print("  correctly and the 'markup' was our own model error.")
    else:
        print("  VERDICT: between the two. Neither story is clean; treat any")
        print("  combo edge claim as unproven.")


if __name__ == "__main__":
    main()
