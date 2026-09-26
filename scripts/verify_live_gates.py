"""Replay real reference data through the REAL rule and count what it admits.

WHY THE CORPUS FIT IS NOT ENOUGH. `brti_decision_points` stores nine derived
features; the live rule reads fourteen. `brti_retrace` and `brti_choppiness`
are not in the corpus at all, so a set fitted there is fitted with those gates
ABSENT - and silver and SOL ran with `require_measurable_retrace: true`
inherited from BTC, which refuses a setup whose recent window holds no advance
to give back. That is the normal state on a level-maintenance instrument. A
corpus fit cannot see that gate, so it cannot see that gate blocking
everything, which is one of the reasons the live qualified count was zero while
the corpus said 7.2%.

So this does not re-fit anything. It takes the config as deployed, pulls each
market's real per-second reference series from `/live_data/events/...`,
recomputes features with the same `features_from_series` the service calls at
the same decision cadence, and runs `rule.check_facts` - every gate, in its
live form. Then it reports, for the markets it saw:

    let through   at least one decision minute passed EVERY enabled gate
    blocked       none did, and which gate was responsible
    outcome       settled W-L for each group, from the market result

The W-L on the blocked group is the number that answers "does it block winners".
A gate set that blocks at a higher win rate than it admits is choosing badly,
however good its residual looked on a corpus that could not see all of it.

Prices come from the stored candles, so the admit decision uses the same ask
the service would have paid. No fees, per the operator's standing instruction.
"""

import argparse
import asyncio
import json
import sqlite3
import sys
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.brti import KalshiBRTI, features_from_series  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.kalshi_brti import KalshiBRTIRule  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DECISION_SECONDS = [780, 720, 660, 600, 540, 480, 420, 360, 300, 240, 180, 120]


def interval(rows, draws=4000, seed=11):
    """Day-clustered, because points inside one day share a price path. 54
    markets of one afternoon is not 54 independent observations, and reading a
    point estimate off that sample without an interval is what made an
    80-market replay look like it contradicted a 445-market fit."""
    byday = defaultdict(list)
    for r in rows:
        byday[r["day"]].append((1.0 if r["won"] else 0.0) - r["ask"])
    days = list(byday)
    if len(days) < 4:
        return None
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        vals = [x for d in pick for x in byday[d]]
        out.append(sum(vals) / len(vals))
    out.sort()
    flat = [x for v in byday.values() for x in v]
    return (statistics.mean(flat), out[int(draws * 0.025)],
            out[int(draws * 0.975) - 1], len(days))


def rule_for(config: str) -> KalshiBRTIRule:
    cfg = {k: v for k, v in
           json.loads((ROOT / config).read_text(encoding="utf-8")).items()
           if not k.startswith("_")}
    cfg.pop("enabled", None)
    return KalshiBRTIRule(**cfg)


def book_for(market_db: str) -> dict:
    db = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    book = {}
    for t, ts, bid, ask in db.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
        if bid is None or ask is None:
            continue
        ms = ts * 1000 if ts < 1e11 else ts
        book[(t, ms // 60000 * 60000)] = (bid, ask)
    db.close()
    return book


def markets(market_db: str, limit: int) -> list[dict]:
    db = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(
        "SELECT ticker, open_ms, close_ms, floor_strike, result "
        "  FROM markets "
        " WHERE result IN ('yes','no') AND floor_strike IS NOT NULL "
        " ORDER BY close_ms DESC")]
    db.close()
    return rows[:limit]


async def run(args) -> None:
    settings = Settings()
    rule = rule_for(args.config)
    book = book_for(args.market)
    todo = markets(args.market, args.limit)
    print(f"{args.asset.upper()}  {args.config}")
    print(f"  replaying {len(todo)} most recent settled markets "
          f"through the deployed rule")

    client = KalshiBRTI(settings.kalshi_base_url, timeout=25)
    through, every, blocked, skipped = [], [], [], 0
    culprits, first_fail = Counter(), Counter()
    try:
        for i, m in enumerate(todo, 1):
            event = m["ticker"].rsplit("-", 1)[0]
            day = datetime.fromtimestamp(
                m["close_ms"] / 1000, timezone.utc).date()
            try:
                series = await client.series(event)
            except Exception:  # noqa: BLE001 - one bad event is not fatal
                skipped += 1
                continue
            if not series:
                skipped += 1
                continue
            series.sort()
            passed_any = False
            fails_this_market = Counter()
            evaluated = 0
            last_won = last_ask = None
            for remaining in DECISION_SECONDS:
                # THE ENTRY WINDOW IS NOT A GATE IN `check_facts`. It is
                # enforced in `main.py` before the rule is consulted and again
                # in `rule.matches`, so calling `check_facts` directly admits
                # minutes the service never acts on. Skipping this check made
                # an earlier version of this script evaluate 780, 300, 240, 180
                # and 120 seconds on a config whose window is 700-400, and the
                # late minutes - where the ask is extreme and the edge is a
                # different thing - dragged gold's admitted residual from
                # +0.0900 to +0.0130. The script was measuring a configuration
                # that is not deployed.
                if not (rule.entry_to_seconds <= remaining
                        <= rule.entry_from_seconds):
                    continue
                cutoff = m["close_ms"] - remaining * 1000
                upto = [(t, v) for t, v in series if t <= cutoff]
                if len(upto) < 120:
                    continue
                f = features_from_series(event, upto, m["floor_strike"], cutoff)
                if f is None:
                    continue
                q = book.get((m["ticker"], cutoff // 60000 * 60000))
                if q is None:
                    continue
                yes_bid, yes_ask = q
                yes_bid = yes_bid / 100 if yes_bid > 1 else yes_bid
                yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
                ask = rule.ask_for(f, yes_bid, yes_ask)
                evaluated += 1
                # PRICE THE BLOCKED GROUP AT THE SAME POINT IN THE WINDOW.
                # Sampling it at the last evaluated minute put its mean ask at
                # 0.85-0.94 against the admitted group's 0.66-0.71, because by
                # 120s left the market has mostly resolved. Comparing the two
                # residuals then measures the sampling instant, not the gates.
                # So the counterfactual is taken at the FIRST minute inside the
                # entry window - exactly where an admitted market is taken.
                if last_won is None:
                    last_won = (m["result"] == "yes") == (f.side == "UP")
                    last_ask = ask
                facts = rule.check_facts(f, ask, remaining)
                bad = [x["name"] for x in facts
                       if x.get("enabled", True) and not x["passed"]]
                if not bad:
                    won = (m["result"] == "yes") == (f.side == "UP")
                    row = {"ticker": m["ticker"], "won": won, "ask": ask,
                           "remaining": remaining, "day": day}
                    # TWO ESTIMATORS, because they answer different questions
                    # and mixing them silently is how a warning becomes an
                    # artefact. The FIRST qualifying minute is what the service
                    # acts on - one alert per market. EVERY qualifying minute is
                    # what the corpus fit scored. A gap between them is about
                    # WHEN inside the window the edge sits, not about whether
                    # it exists.
                    if not passed_any:
                        through.append(row)
                    every.append(row)
                    passed_any = True
                    continue
                for name in bad:
                    fails_this_market[name] += 1
            if evaluated == 0:
                skipped += 1
                continue
            if not passed_any:
                # Attribute to the gate that failed at the MOST decision
                # minutes: the one that would have had to change for this
                # market to be seen at all.
                #
                # The blocked group needs an outcome too, and it has one: the
                # reference implies a SIDE at every minute whether or not the
                # gates let it through, so "would this call have won" is
                # answerable. Without it the report says what was refused but
                # not whether refusing was right, which is the only question
                # that matters about a blocked market.
                blocked.append({"ticker": m["ticker"], "result": m["result"],
                                "won": last_won, "ask": last_ask,
                                "day": day})
                for name, n in fails_this_market.items():
                    culprits[name] += n
                if fails_this_market:
                    first_fail[fails_this_market.most_common(1)[0][0]] += 1
            if i % 25 == 0:
                print(f"    {i}/{len(todo)}  through {len(through)}  "
                      f"blocked {len(blocked)}", flush=True)
    finally:
        await client.close()

    seen = len(through) + len(blocked)
    if not seen:
        print("  nothing replayable - no candles overlap the reference series")
        return
    print(f"\n  RESULT over {seen} replayable markets "
          f"({skipped} had no usable reference or price)")
    print(f"    let through   {len(through):>4}  {len(through)/seen:>5.1%}")
    print(f"    blocked       {len(blocked):>4}  {len(blocked)/seen:>5.1%}")

    def line(label, rows):
        if not rows:
            return
        w = sum(1 for x in rows if x["won"])
        ask = sum(x["ask"] for x in rows) / len(rows)
        iv = interval(rows)
        ci = (f"  [{iv[1]:+.4f}, {iv[2]:+.4f}] over {iv[3]} days"
              if iv else "  (too few days for an interval)")
        print(f"  {label:<28}{w}W-{len(rows) - w}L = {w/len(rows):>5.1%} "
              f"at ask {ask:.3f}  residual {w/len(rows) - ask:+.4f}{ci}")

    if through:
        print()
        # TWO ESTIMATORS. The first is what the service does - one alert per
        # market, at the earliest qualifying minute. The second is what the
        # corpus fit scored. Reporting only one of them is how an 80-market
        # replay came to look like it contradicted a 445-market fit.
        line("LET THROUGH, first minute", through)
        line("LET THROUGH, every minute", every)
    else:
        print("\n  LET THROUGH   NOTHING. This is the silver/SOL defect: a "
              "config that\n                admits nothing cannot be learned "
              "from and cannot be wrong.")

    graded = [x for x in blocked if x["won"] is not None]
    if graded:
        line("BLOCKED, same window point", graded)
        print("                The blocked side and price are taken at the "
              "first minute inside the")
        print("                entry window, the same point an admitted market "
              "is taken, so the two")
        print("                residuals are comparable. Refusing is only "
              "right if this scores WORSE.")
    if first_fail:
        print("\n  WHAT BLOCKED THE REST   (gate that failed at the most "
              "decision minutes)")
        for name, n in first_fail.most_common(8):
            print(f"    {name:<34} {n:>4}  {n/max(1,len(blocked)):>5.1%} "
                  f"of blocked markets")
    if culprits:
        print("\n  GATE FAILURES across all evaluated minutes")
        for name, n in culprits.most_common(10):
            print(f"    {name:<34} {n:>5}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--asset", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--market", required=True)
    p.add_argument("--limit", type=int, default=120)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
