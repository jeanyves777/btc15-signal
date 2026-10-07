"""Would a COMBO recovery have beaten the current single-leg recovery?

THE PROPOSAL, operator 2026-09-27. Every current recovery rule stays exactly as
deployed in `main.loss_step_size`:

    armed by a losing market, live for 5 settled markets, fires ONCE per loss,
    only on a trade whose ask is inside 0.70-0.79, sized from a $2.00 budget,
    and it never causes a trade - it only sizes one going out anyway.

What changes is the ORDER. When the recovery fires, instead of buying more of
the one instrument, it buys a COMBO of that instrument plus ANOTHER instrument
whose own rule qualified at the same moment, with the same $2.00 budget. And one
check is added, the only new one:

    the combo's profit if right must net the loss being recovered to zero or
    beat it:  contracts x (1 - price) >= loss

If no qualified partner passes that check, the trade goes out at base size and
the recovery stays armed - exactly what the current rule does when the price is
outside the band.

WHY THE CHECK MATTERS. The current recovery buys 2 contracts at 0.70-0.79, which
wins 0.42-0.60 if right, while the loss it follows is usually a 1-contract loss
of 0.70-0.90. As deployed it therefore cannot net a typical loss to zero in one
trade; it only dents it. Two legs near 0.80 cost about 0.64, so $2 buys 3
contracts that win about 1.08.

REPLAYED ON WHAT ACTUALLY HAPPENED, and nothing from earlier findings is assumed.
The entries are the bot's real trades, one per window, read the way
`Store.settled_bot_markets` reads them - `trade_proposals` joined to the broker's
`settlements` - priced at the decision ask recorded in `decision_records`. The
partner is another instrument's REAL rule-qualified decision from its own store,
taken as of the recovery's decision instant and never later: a decision recorded
after that moment is a lookahead and is not used. A partner reading older than
STALE_S is treated as absent rather than trusted.

SIZING IS REPLAYED UNDER TODAY'S RULES, not read from history. `count > 1` is not
a clean recovery marker: BTC ran a 2-contract base earlier in the week and an
upfront recovery path before the loss step, so half its trades are upsized for
reasons that no longer apply. Both policies start from base 1.

EVERYTHING IS HELD TO SETTLEMENT and no fees are charged, per the operator's
standing instruction. Real early cash-outs are not modelled for either policy;
their count is printed so the omission is visible.

THE SPAN IS WHERE A PARTNER COULD HAVE EXISTED. Other instruments only began
recording decisions part-way through the week, and a combo with no recorded
partner is a gap in the LOGS, not a verdict on the rule. Each instrument is
replayed only from the first moment another instrument was recording.

    python scripts/recovery_combo_replay.py
    python scripts/recovery_combo_replay.py --markup 1.10
"""

import argparse
import bisect
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STORES = {
    "BTC": "btc15.db", "ETH": "eth15.db", "SOL": "sol15.db",
    "XRP": "xrp15.db", "NEAR": "near15.db",
    "GOLD": "gold15.db", "SILVER": "silver15.db",
}
TRADERS = ("BTC", "ETH", "SOL")

# The deployed rule, read from config.py on 2026-09-27.
BAND = (0.70, 0.79)
WAIT_MARKETS = 5
BUDGET = 2.00
CAP = 8
BASE = 1
STALE_S = 120
# THE ALLOWED COMBO, operator 2026-09-27: "the combo must only be SOL and BTC".
# A BTC recovery may pair only with a qualified SOL signal and a SOL recovery
# only with a qualified BTC one. An instrument outside the pair keeps today's
# single-leg recovery under BOTH policies, so it cancels out of the comparison
# and is not reported. None allows any instrument.
PAIR = ("BTC", "SOL")
# How a window with several losing markets is recovered: "sum" nets all of
# them, "last" nets only the largest single market. See simulate_account.
LOSS_MODE = "sum"


# THE PAIRING, operator 2026-09-27, final: "SOL is the only one that combines
# with both" and "SOL only combines with BTC". Each instrument recovers its OWN
# loss; BTC and ETH each pair only
# with SOL, SOL pairs only with BTC, and BTC+ETH is never a recovery combo. This
# replaces PAIR when set. An earlier reading of the operator - that ETH losses
# were recovered by BTC+SOL and ETH never traded in a recovery - was wrong; it is
# kept as `--mode account` only so that result stays reproducible.
ALLOWED = {"BTC": ("SOL",), "ETH": ("SOL",), "SOL": ("BTC",)}


def partners_for(asset):
    """Which instruments may be the second leg of this instrument's recovery."""
    if ALLOWED is not None:
        return tuple(ALLOWED.get(asset, ()))
    if PAIR:
        return tuple(k for k in PAIR if k != asset) if asset in PAIR else ()
    return tuple(k for k in STORES if k != asset)


def day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


def contracts_for_budget(budget, price, minimum=1):
    """Same as validation.contracts_for_budget: whole contracts, round down."""
    if price <= 0:
        return minimum
    return max(minimum, int(budget / price))


def load_entries(asset):
    """The bot's real trades, one per window, as settled_bot_markets reads them."""
    con = sqlite3.connect(f"file:{ROOT / STORES[asset]}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT p.window_open, p.created_at, p.ticker, p.side, p.count, "
        "       p.fill_price, p.status, s.market_result, d.ask, d.created_at "
        "  FROM trade_proposals p "
        "  JOIN settlements s ON s.ticker = p.ticker "
        "  LEFT JOIN decision_records d "
        "         ON d.proposal_id = p.id AND d.action = 'ENTERED' "
        " WHERE p.strategy = 'primary' AND p.fill_price IS NOT NULL "
        "   AND s.market_result IN ('yes','no') "
        " ORDER BY p.window_open, p.created_at").fetchall()
    con.close()
    by_window = {}
    for (w, created, ticker, side, count, fill, status, result,
         ask, decided) in rows:
        # Latest filled proposal per window, as settled_bot_markets keeps it.
        by_window[int(w)] = {
            "window": int(w), "ticker": ticker, "side": side,
            "ask": float(ask) if ask is not None else float(fill),
            "fill": float(fill), "count": float(count or 0),
            "t": int(decided or created),
            "won": (result == "yes") == (side == "UP"),
            "exited": status == "exited",
        }
    return [by_window[w] for w in sorted(by_window)]


def load_partner_book():
    """{asset: {window: ([decided_ms...], [(qualified, side, ask, won)...])}}"""
    book, first_seen = {}, {}
    for asset, db in STORES.items():
        path = ROOT / db
        if not path.exists():
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        per = defaultdict(lambda: ([], []))
        first = None
        for w, ms, q, side, ask, won in con.execute(
                "SELECT window_open, decided_ms, base_qualified, side, ask, won "
                "  FROM intelligence_decisions "
                " WHERE ask IS NOT NULL ORDER BY window_open, decided_ms"):
            first = ms if first is None else min(first, ms)
            times, vals = per[int(w)]
            times.append(int(ms))
            vals.append((bool(q), side, float(ask),
                         None if won is None else bool(won)))
        con.close()
        book[asset] = dict(per)
        first_seen[asset] = first
    return book, first_seen


# DOES THE PARTNER LEG HAVE TO BE RULE-QUALIFIED? Operator, 2026-09-27: "the
# recovery combo should only need one qualified side; the other asset should
# trade whatever its signal shows, so there is always a match." False takes the
# partner on the side its signal showed at that instant, qualified or not. True
# is the earlier rule, kept so both can be compared.
PARTNER_MUST_QUALIFY = False


def partner_at(book, asset, window, t):
    """The partner's signal AS OF t - never a decision recorded later."""
    slot = book.get(asset, {}).get(window)
    if not slot:
        return None
    times, vals = slot
    i = bisect.bisect_right(times, t) - 1
    if i < 0:
        return None
    if (t - times[i]) / 1000.0 > STALE_S:
        return None
    qualified, side, ask, won = vals[i]
    if PARTNER_MUST_QUALIFY and not qualified:
        return None
    if won is None or not 0.02 <= ask <= 0.98:
        return None
    return {"asset": asset, "side": side, "ask": ask, "won": won,
            "decided_ms": times[i]}


def sign(side):
    return "+" if side == "UP" else "-"


# THE OPERATOR'S BOUNDED CHAIN (2026-09-27). Without it, a losing recovery combo
# is simply a new loss, the recovery re-arms, and the net-zero check then demands
# the NEXT combo clear that larger loss - which only a cheaper, less likely combo
# can do. On the real record that spiralled: three SOL recovery combos lost in a
# row, -6.00 against today's recovery over three days. The chain bounds it:
#   step 1  clear L0; a win ends it
#   step 2  after a step-1 loss, clear at least CHAIN_SECOND_SHARE of that loss
#   step 3  clear L0 again, then STOP whatever happens
# Each step has the usual 5-market life from the event before it.
CHAIN = True
CHAIN_SECOND_SHARE = 0.50


def _partner_combos(asset, e, book, markup):
    cands = []
    for other in partners_for(asset):
        p = partner_at(book, other, e["window"], e["t"])
        if p is None:
            continue
        assert p["decided_ms"] <= e["t"], "partner from the future"
        price = e["ask"] * p["ask"] * markup
        if price >= 1.0:
            continue
        n = min(contracts_for_budget(BUDGET, price), CAP)
        cands.append((p, price, n, n * (1.0 - price)))
    return cands


def simulate_chain(asset, entries, book, markup):
    results, episodes = [], []
    ep = None   # {"step", "target", "l0", "since"}
    for e in entries:
        pnl = BASE * ((1.0 if e["won"] else 0.0) - e["ask"])
        kind, note, fired = "base", "", False
        if ep is not None and ep["since"] >= WAIT_MARKETS:
            ep = None
        if ep is not None and partners_for(asset) \
                and BAND[0] <= e["ask"] <= BAND[1]:
            cands = _partner_combos(asset, e, book, markup)
            ok = [c for c in cands if c[3] >= ep["target"]]
            if not cands:
                note = "no partner at that moment"
            elif not ok:
                note = "partner failed the check"
            else:
                p, price, n, profit = max(ok, key=lambda c: c[0]["ask"])
                both = e["won"] and p["won"]
                pnl = n * ((1.0 if both else 0.0) - price)
                kind, fired = "combo", True
                episodes.append({
                    "loss": ep["target"], "window": e["window"],
                    "legs": f"{asset}{sign(e['side'])} & "
                            f"{p['asset']}{sign(p['side'])}",
                    "price": price, "n": n, "pnl": pnl,
                    "netted": pnl >= ep["target"], "won": both,
                    "step": ep["step"]})
                if ep["step"] == 1:
                    ep = None if both else {
                        "step": 2, "target": CHAIN_SECOND_SHARE * n * price,
                        "l0": ep["l0"], "since": 0}
                elif ep["step"] == 2:
                    ep = {"step": 3, "target": ep["l0"], "l0": ep["l0"],
                          "since": 0}
                else:
                    ep = None
        if not fired:
            if ep is not None:
                ep["since"] += 1
            # A base loss starts an episode, or supersedes one still waiting at
            # step 1 - as the deployed rule arms on the most recent loss. It
            # does not interrupt a chain already past step 1.
            if pnl < 0 and (ep is None or ep["step"] == 1):
                ep = {"step": 1, "target": -pnl, "l0": -pnl, "since": 0}
        results.append({"window": e["window"], "pnl": pnl, "kind": kind,
                        "note": note})
    return results, episodes


def simulate(asset, entries, policy, book, markup):
    if policy == "B" and CHAIN:
        return simulate_chain(asset, entries, book, markup)
    results, spent, episodes = [], set(), []
    for e in entries:
        armed, since = None, 0
        for idx, r in enumerate(reversed(results)):
            if r["pnl"] < 0:
                armed, since = r, idx
                break
        pnl = BASE * ((1.0 if e["won"] else 0.0) - e["ask"])
        kind, note = "base", ""
        if armed is not None and since < WAIT_MARKETS \
                and armed["window"] not in spent:
            loss = -armed["pnl"]
            single_leg = policy == "A" or not partners_for(asset)
            if not BAND[0] <= e["ask"] <= BAND[1]:
                note = "waiting: ask out of band"
            elif single_leg:
                n = min(contracts_for_budget(BUDGET, e["ask"]), CAP)
                if n > BASE:
                    pnl = n * ((1.0 if e["won"] else 0.0) - e["ask"])
                    kind = "recovery"
                    spent.add(armed["window"])
                    episodes.append({
                        "loss": loss, "window": e["window"],
                        "legs": f"{asset}{sign(e['side'])}",
                        "price": e["ask"], "n": n, "pnl": pnl,
                        "netted": pnl >= loss, "won": e["won"]})
            else:
                cands = []
                for other in partners_for(asset):
                    p = partner_at(book, other, e["window"], e["t"])
                    if p is None:
                        continue
                    # A LOOKAHEAD GUARD THAT CAN FAIL, not a comment.
                    assert p["decided_ms"] <= e["t"], "partner from the future"
                    price = e["ask"] * p["ask"] * markup
                    if price >= 1.0:
                        continue
                    n = min(contracts_for_budget(BUDGET, price), CAP)
                    profit = n * (1.0 - price)
                    cands.append((p, price, n, profit))
                ok = [c for c in cands if c[3] >= loss]
                if not cands:
                    note = "no qualified partner at that moment"
                elif not ok:
                    note = "partner failed the net-zero check"
                else:
                    # The most likely partner among those that clear the check:
                    # the combo must win BOTH legs.
                    p, price, n, profit = max(ok, key=lambda c: c[0]["ask"])
                    both = e["won"] and p["won"]
                    pnl = n * ((1.0 if both else 0.0) - price)
                    kind = "combo"
                    spent.add(armed["window"])
                    episodes.append({
                        "loss": loss, "window": e["window"],
                        "legs": f"{asset}{sign(e['side'])} & "
                                f"{p['asset']}{sign(p['side'])}",
                        "price": price, "n": n, "pnl": pnl,
                        "netted": pnl >= loss, "won": both})
        results.append({"window": e["window"], "pnl": pnl, "kind": kind,
                        "note": note})
    return results, episodes


def run(markup, verbose=True):
    book, first_seen = load_partner_book()
    grand = {"A": 0.0, "B": 0.0}
    all_eps = {"A": [], "B": []}
    perday = defaultdict(lambda: [0.0, 0.0])
    lines = []
    for asset in TRADERS:
        if not partners_for(asset):
            continue  # single-leg under both policies: identical, cancels
        entries = load_entries(asset)
        # THE SPAN STARTS WHEN AN ALLOWED PARTNER WAS RECORDING. BTC and ETH
        # can only be judged from SOL's first logged decision - before that a
        # missing partner is a missing LOG, not an unqualified signal.
        others = [first_seen[k] for k in partners_for(asset)
                  if first_seen.get(k)]
        if not entries or not others:
            continue
        start = min(others)
        entries = [e for e in entries if e["t"] >= start]
        if not entries:
            continue
        ra, ea = simulate(asset, entries, "A", book, markup)
        rb, eb = simulate(asset, entries, "B", book, markup)
        ta = sum(r["pnl"] for r in ra)
        tb = sum(r["pnl"] for r in rb)
        grand["A"] += ta
        grand["B"] += tb
        all_eps["A"] += ea
        all_eps["B"] += eb
        for e, x, y in zip(entries, ra, rb):
            perday[day(e["window"])][0] += x["pnl"]
            perday[day(e["window"])][1] += y["pnl"]
        losses = sum(1 for r in ra if r["pnl"] < 0)
        exited = sum(1 for e in entries if e["exited"])
        skip = defaultdict(int)
        for r in rb:
            if r["kind"] == "base" and r["note"] \
                    and not r["note"].startswith("waiting"):
                skip[r["note"]] += 1
        lines.append(
            f"\n  {asset}: {len(entries)} real trades "
            f"{day(entries[0]['window'])}..{day(entries[-1]['window'])}, "
            f"{losses} losing markets, {exited} actually cashed out early "
            f"(held to settlement here)")
        lines.append(
            f"    A  single-leg recovery   fired {len(ea):>3}   "
            f"won {sum(1 for x in ea if x['won']):>3}   "
            f"netted the loss {sum(1 for x in ea if x['netted']):>3}   "
            f"instance P&L {ta:+.2f}")
        lines.append(
            f"    B  combo recovery        fired {len(eb):>3}   "
            f"won {sum(1 for x in eb if x['won']):>3}   "
            f"netted the loss {sum(1 for x in eb if x['netted']):>3}   "
            f"instance P&L {tb:+.2f}")
        if skip:
            lines.append("       B held back: " + ", ".join(
                f"{k} x{v}" for k, v in skip.items()))
    if verbose:
        for line in lines:
            print(line)
    return grand, all_eps, perday


def simulate_account(markup):
    """ACCOUNT-LEVEL RECOVERY, the operator's rule of 2026-09-27.

    "Even after an ETH loss, ETH will not be in it - BTC and SOL as well." So:

      * ANY losing market - BTC, ETH or SOL - arms the recovery;
      * the recovery is always a BTC+SOL combo, placed when a BTC or SOL trade
        is going out anyway with its ask inside 0.70-0.79 AND the other of the
        pair is rule-qualified at that same instant;
      * the combo must clear the net-zero check against the arming loss;
      * ETH never upsizes; nothing else changes.

    Two readings had to be chosen, and both are stated so they can be argued:

      * THE 5-MARKET LIFE IS COUNTED IN WINDOWS. Per instrument, one market is
        one 15-minute window. Across three instruments one window can settle
        three markets, so counting markets would shorten the life to about two
        windows. Counting distinct settled windows keeps it at the ~75 minutes
        the deployed rule gives.
      * A WINDOW WITH MORE THAN ONE LOSING MARKET IS ONE EPISODE, and the loss to
        recover is their sum - they settled together, and recovering only one of
        them would net nothing to zero.

    Policy A is unchanged: each instrument's own single-leg loss step.
    """
    book, first_seen = load_partner_book()
    span = max(first_seen["BTC"], first_seen["SOL"])
    per_asset = {a: [e for e in load_entries(a) if e["t"] >= span]
                 for a in TRADERS}

    # ---- policy A: as deployed, per instrument
    a_pnl, a_eps = [], []
    for asset, entries in per_asset.items():
        if not entries:
            continue
        res, eps = simulate(asset, entries, "A", book, markup)
        for e, r in zip(entries, res):
            a_pnl.append((e["window"], asset, r["pnl"]))
        a_eps += eps

    # ---- policy B: account-level, BTC+SOL combo only
    stream = sorted(((e["window"], e["t"], a, e)
                     for a, es in per_asset.items() for e in es),
                    key=lambda x: (x[0], x[1]))
    window_losses = defaultdict(float)   # window -> summed loss of losing mkts
    settled_windows = []                 # windows with any settled market
    spent = set()
    b_pnl, b_eps = [], []
    skips = defaultdict(int)
    pair = {"BTC": "SOL", "SOL": "BTC"}
    for window, t, asset, e in stream:
        prior = [w for w in settled_windows if w < window]
        armed, since = None, 0
        for idx, w in enumerate(reversed(prior)):
            if window_losses.get(w, 0.0) > 0:
                armed, since = w, idx
                break
        pnl = BASE * ((1.0 if e["won"] else 0.0) - e["ask"])
        if (asset in pair and armed is not None and since < WAIT_MARKETS
                and armed not in spent):
            loss = window_losses[armed]
            if not BAND[0] <= e["ask"] <= BAND[1]:
                pass  # waiting, exactly as deployed
            else:
                p = partner_at(book, pair[asset], window, t)
                if p is None:
                    skips["the other of BTC/SOL not qualified then"] += 1
                else:
                    assert p["decided_ms"] <= t, "partner from the future"
                    price = e["ask"] * p["ask"] * markup
                    n = min(contracts_for_budget(BUDGET, price), CAP)
                    profit = n * (1.0 - price)
                    if price >= 1.0 or profit < loss:
                        skips["failed the net-zero check"] += 1
                    else:
                        both = e["won"] and p["won"]
                        pnl = n * ((1.0 if both else 0.0) - price)
                        spent.add(armed)
                        legs = (f"BTC{sign(e['side'] if asset == 'BTC' else p['side'])}"
                                f" & SOL{sign(e['side'] if asset == 'SOL' else p['side'])}")
                        b_eps.append({"window": window, "legs": legs,
                                      "price": price, "n": n, "pnl": pnl,
                                      "loss": loss, "netted": pnl >= loss,
                                      "won": both, "trigger": asset})
        b_pnl.append((window, asset, pnl))
        if pnl < 0:
            if LOSS_MODE == "sum":
                window_losses[window] += -pnl
            else:
                window_losses[window] = max(window_losses[window], -pnl)
        if not settled_windows or settled_windows[-1] != window:
            settled_windows.append(window)
    return a_pnl, a_eps, b_pnl, b_eps, skips, span


def main_account(markup):
    a_pnl, a_eps, b_pnl, b_eps, skips, span = simulate_account(markup)
    print("=" * 84)
    print(f"ACCOUNT-LEVEL RECOVERY - any loss, recovered by a BTC+SOL combo "
          f"(combo at {markup:.2f}x product)")
    print("=" * 84)
    print(f"  replayed from {datetime.fromtimestamp(span / 1000, timezone.utc):%m-%d %H:%M} "
          f"UTC, when both BTC and SOL were recording decisions")
    by_asset = defaultdict(lambda: [0, 0.0, 0.0])
    for w, a, p in a_pnl:
        by_asset[a][0] += 1
        by_asset[a][1] += p
    for w, a, p in b_pnl:
        by_asset[a][2] += p
    print(f"\n  {'instrument':<11}{'trades':>7}{'A P&L':>9}{'B P&L':>9}")
    for a in TRADERS:
        n, pa, pb = by_asset[a]
        print(f"  {a:<11}{n:>7}{pa:>+9.2f}{pb:>+9.2f}")
    ta = sum(p for _, _, p in a_pnl)
    tb = sum(p for _, _, p in b_pnl)
    print(f"  {'ACCOUNT':<11}{'':>7}{ta:>+9.2f}{tb:>+9.2f}   B-A {tb - ta:+.2f}")

    print(f"\n  A  single-leg recoveries: {len(a_eps)} fired, "
          f"{sum(1 for x in a_eps if x['won'])} won, "
          f"{sum(1 for x in a_eps if x['netted'])} netted the loss")
    for x in sorted(a_eps, key=lambda x: x["window"]):
        t = datetime.fromtimestamp(x["window"] / 1000, timezone.utc)
        print(f"     {t:%m-%d %H:%M}  {x['legs']:<8} {x['n']} @ {x['price']:.3f}"
              f"  loss {x['loss']:.2f}  {'WON ' if x['won'] else 'LOST'} "
              f"{x['pnl']:+.2f}")
    print(f"\n  B  BTC+SOL combos: {len(b_eps)} fired, "
          f"{sum(1 for x in b_eps if x['won'])} won, "
          f"{sum(1 for x in b_eps if x['netted'])} netted the loss")
    for x in sorted(b_eps, key=lambda x: x["window"]):
        t = datetime.fromtimestamp(x["window"] / 1000, timezone.utc)
        print(f"     {t:%m-%d %H:%M}  {x['legs']:<12} {x['n']} @ {x['price']:.3f}"
              f"  loss {x['loss']:.2f}  {'WON ' if x['won'] else 'LOST'} "
              f"{x['pnl']:+.2f}  {'netted' if x['netted'] else 'short'}")
    if skips:
        print("     held back: " + ", ".join(f"{k} x{v}"
                                             for k, v in skips.items()))

    perday = defaultdict(lambda: [0.0, 0.0])
    for w, _, p in a_pnl:
        perday[day(w)][0] += p
    for w, _, p in b_pnl:
        perday[day(w)][1] += p
    print("\n  BY DAY")
    for d in sorted(perday):
        a, b = perday[d]
        print(f"    {d}   A {a:+7.2f}   B {b:+7.2f}   B-A {b - a:+7.2f}")
    return ta, tb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("per-instrument", "account"),
                    default="per-instrument",
                    help="per-instrument: each instrument recovers its own "
                         "loss, SOL partnering BTC and ETH (the operator's "
                         "rule); account: an earlier misreading, kept "
                         "reproducible")
    ap.add_argument("--markup", type=float, default=1.00,
                    help="combo price as a multiple of the legs' product; "
                         "1.00 is the combo orderbook")
    args = ap.parse_args()
    if args.mode == "account":
        main_account(args.markup)
        return

    print("=" * 84)
    print(f"RECOVERY AS A COMBO - replayed on real trades and real signals "
          f"(combo at {args.markup:.2f}x product)")
    print("=" * 84)
    grand, all_eps, perday = run(args.markup)

    print("\n  EVERY RECOVERY, as each policy placed it")
    for pol in ("A", "B"):
        eps = sorted(all_eps[pol], key=lambda x: x["window"])
        print(f"  --- policy {pol} ---")
        if not eps:
            print("    none fired")
        for x in eps:
            t = datetime.fromtimestamp(x["window"] / 1000, timezone.utc)
            print(f"    {t:%m-%d %H:%M}  {x['legs']:<14} {x['n']} @ "
                  f"{x['price']:.3f}  loss to recover {x['loss']:.2f}  "
                  f"{'WON ' if x['won'] else 'LOST'}  {x['pnl']:+.2f}  "
                  f"{'netted' if x['netted'] else 'short'}")

    print("\n  BY DAY (instance P&L, both policies, same trades)")
    for d in sorted(perday):
        a, b = perday[d]
        print(f"    {d}   A {a:+7.2f}   B {b:+7.2f}   B-A {b - a:+7.2f}")
    diffs = [v[1] - v[0] for v in perday.values()]
    print(f"\n  TOTAL   A {grand['A']:+.2f}   B {grand['B']:+.2f}   "
          f"difference {grand['B'] - grand['A']:+.2f}   "
          f"over {len(perday)} days")
    if len(diffs) > 1:
        print(f"  B beat A on {sum(1 for v in diffs if v > 0)} of "
              f"{len(diffs)} days (median day {statistics.median(diffs):+.2f})")

    # How much dearer than the product can the combo be before B stops beating
    # A? The venue decides the answer, so this is printed rather than assumed.
    lo, hi = 0.80, 2.50
    base_gap = grand["B"] - grand["A"]
    if base_gap > 0:
        for _ in range(30):
            mid = (lo + hi) / 2
            g, _, _ = run(mid, verbose=False)
            if g["B"] - g["A"] > 0:
                lo = mid
            else:
                hi = mid
        print(f"  B stays ahead of A up to a combo price of about "
              f"{lo:.2f}x the product")


if __name__ == "__main__":
    main()
