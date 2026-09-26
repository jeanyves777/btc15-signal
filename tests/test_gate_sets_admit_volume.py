"""A deployed gate set must admit a learnable share of its own corpus.

THE TEST THAT WAS MISSING. On 2026-09-25 silver and SOL each ran seven
thresholds, every one picked from its own best bucket in isolation. Each was
defensible alone. Intersected they admitted NOTHING - 423 and 422 live
evaluations, zero qualified, every market blocked - and nobody noticed for days
because no test asked the one question that matters about a SET rather than a
gate: does the combination let anything through.

Volume is not a preference here, it is what makes the configuration
correctable. The confidence arms need `min_evidence` observations before they
can act; a set admitting 2% never reaches that, so the learned layer stays
inert and the configuration can never be revised by evidence. A gate set that
admits nothing cannot be learned from and cannot be shown wrong.

This runs the REAL rule - `KalshiBRTIRule.check_facts`, every gate in its live
form including retrace - over each instrument's own brti-4 corpus, and asserts
two things:

    the set admits at least MIN_SHARE of decision points
    the set does not admit at a LOWER win rate than the corpus overall

The second is the other half of the operator's requirement: a gate set that
blocks winners and admits losers is worse than no gates, however healthy its
volume looks.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.kalshi_brti import KalshiBRTIRule  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# A COLLAPSE GUARD, DELIBERATELY BELOW THE FIT'S FLOOR. The fit itself was run
# with --min-share 0.15 and all three configs record that number; this guard sits
# at 0.10 so it catches the failure it was written for - a set that admits
# essentially nothing, as silver's 0.05% and SOL's 2.16% did - without flapping
# on ordinary variation as a corpus grows. Saying it was "the floor the fit used"
# was simply wrong, and a guard that misstates its own basis invites someone to
# raise or lower it for the wrong reason.
#
# Measured 2026-09-25, in-window: gold 18.2%, silver 15.7%, SOL 20.5%.
MIN_SHARE = 0.10

INSTRUMENTS = {
    "GOLD": ("strategy_kalshi_gold.json", "brti_history_gold_v5.db"),
    "SILVER": ("strategy_kalshi_silver.json", "brti_history_silver_v5.db"),
    "SOL": ("strategy_kalshi_sol.json", "brti_history_sol_v5.db"),
    # XRP's corpus was built after the retrace fix, so there is only one of it.
    "XRP": ("strategy_kalshi_xrp.json", "brti_history_xrp.db"),
}


class Feats:
    """The shape `check_facts` reads, filled from a stored decision point."""

    stale = False

    def __init__(self, row):
        self.value = row["brti_value"]
        self.target = row["target"]
        self.side = row["brti_side"]
        self.samples = row["samples"]
        self.signed_distance_bps = row["signed_distance_bps"]
        self.brti_momentum_bps = row["brti_momentum_bps"]
        self.brti_volatility_bps = row["brti_volatility_bps"]
        self.brti_normalized_distance = row["brti_normalized_distance"]
        self.brti_accel = row["brti_accel"]
        self.brti_held_s = row["brti_held_s"]
        self.brti_rejections = row["brti_rejections"]
        self.brti_retrace = row["brti_retrace"]
        self.brti_choppiness = row["brti_choppiness"]


def rule_for(config):
    cfg = {k: v for k, v in
           json.loads((ROOT / config).read_text(encoding="utf-8")).items()
           if not k.startswith("_")}
    cfg.pop("enabled", None)
    return KalshiBRTIRule(**cfg)


def corpus(db, market_db):
    """Decision points with the ask the service would have paid."""
    m = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    book = {}
    for t, ts, bid, ask in m.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
        if bid is None or ask is None:
            continue
        ms = ts * 1000 if ts < 1e11 else ts
        book[(t, ms // 60000 * 60000)] = (bid, ask)
    m.close()
    b = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    out = []
    for r in b.execute("SELECT * FROM brti_decision_points "
                       " WHERE result IN ('yes','no') "
                       "   AND signed_distance_bps IS NOT NULL"):
        ms = r["close_ms"] - r["remaining_s"] * 1000
        q = book.get((r["ticker"], ms // 60000 * 60000))
        if q is None:
            continue
        yes_bid, yes_ask = q
        yes_bid = yes_bid / 100 if yes_bid > 1 else yes_bid
        yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
        out.append((Feats(r), yes_bid, yes_ask, r["remaining_s"],
                    (r["result"] == "yes") == (r["brti_side"] == "UP")))
    b.close()
    return out


def admit(rule, rows):
    """Exactly the live decision: inside the entry window, every ENABLED gate
    has to pass.

    THE WINDOW IS NOT ONE OF THE GATES. `check_facts` does not check it - the
    service enforces it in `main.py` before consulting the rule, and again in
    `rule.matches`. Calling `check_facts` alone therefore admits minutes that
    are never acted on, which is how a replay script came to score gold's
    admitted group at +0.0130 against the fit's +0.0900: it was evaluating
    780s and 120s on a config whose window is 700-400s."""
    through = []
    for f, bid, ask, remaining, won in rows:
        if not (rule.entry_to_seconds <= remaining <= rule.entry_from_seconds):
            continue
        price = rule.ask_for(f, bid, ask)
        facts = rule.check_facts(f, price, remaining)
        if all(x["passed"] for x in facts if x.get("enabled", True)):
            through.append((price, won))
    return through


MARKETS = {"GOLD": "market_data_kxgold15m.db",
           "SILVER": "market_data_kxsilver15m.db",
           "SOL": "market_data_kxsol15m.db",
           "XRP": "market_data_kxxrp15m.db"}


def loaded(name):
    config, corpus_db = INSTRUMENTS[name]
    db = ROOT / "data" / corpus_db
    market = ROOT / "data" / MARKETS[name]
    if not db.exists() or not market.exists():
        pytest.skip(f"{name}: no corpus at {db}")
    rows = corpus(str(db), str(market))
    if len(rows) < 200:
        pytest.skip(f"{name}: corpus too small ({len(rows)} priced points)")
    return rule_for(config), rows


def in_window(rule, rows):
    return [r for r in rows
            if rule.entry_to_seconds <= r[3] <= rule.entry_from_seconds]


@pytest.mark.parametrize("name", sorted(INSTRUMENTS))
def test_the_deployed_set_admits_a_learnable_share(name):
    rule, rows = loaded(name)
    rows = in_window(rule, rows)
    assert rows, f"{name}: the corpus holds no point inside the entry window"
    through = admit(rule, rows)
    share = len(through) / len(rows)
    assert share >= MIN_SHARE, (
        f"{name} admits {len(through)}/{len(rows)} = {share:.2%} of its own "
        f"corpus, under the {MIN_SHARE:.0%} floor. This is the silver/SOL "
        f"defect: thresholds each defensible alone, intersected into a set "
        f"that lets nothing through and so can never be corrected.")


@pytest.mark.parametrize("name", sorted(INSTRUMENTS))
def test_the_deployed_set_does_not_select_for_losers(name):
    """Admitting at a worse win rate than the corpus overall means the gates
    are choosing badly - the other half of "don't just block or let through
    bad setups"."""
    rule, rows = loaded(name)
    rows = in_window(rule, rows)
    through = admit(rule, rows)
    assert through, f"{name}: nothing admitted"
    # `sum(1 for _, won in through)` counts every row, not the wins. It read
    # as 100% for both instruments and made this assertion vacuous.
    got = sum(1 for _, won in through if won) / len(through)
    base = sum(1 for *_, won in rows if won) / len(rows)
    assert got >= base - 0.02, (
        f"{name} admits at {got:.1%} against a {base:.1%} corpus baseline - "
        f"the gates are selecting worse setups than taking everything")


@pytest.mark.parametrize("name", sorted(INSTRUMENTS))
def test_no_gate_is_a_threshold_nothing_can_fail(name):
    """Gold once showed "Entry checks 8/8" where five thresholds were
    `accel >= -1e9`, `held >= 0s`, `rejections >= 0`, `momentum >= 0.0` and
    `retrace <= 1.0`. Any gate that cannot fail must report itself DISABLED,
    never as a tick - so whatever is still counted has to bind somewhere in
    the corpus."""
    rule, rows = loaded(name)
    counted, ever_failed = set(), set()
    for f, bid, ask, remaining, _ in rows:
        if not (rule.entry_to_seconds <= remaining <= rule.entry_from_seconds):
            continue
        for x in rule.check_facts(f, rule.ask_for(f, bid, ask), remaining):
            if not x.get("enabled", True):
                continue
            # `Reference` is staleness and sample count. A replayed corpus row
            # is never stale - the feed gap that would trip it live leaves no
            # decision point behind - so it cannot fail here for reasons that
            # have nothing to do with the config. It is also the one gate with
            # no threshold to soften.
            if x["name"] == "Reference":
                continue
            counted.add(x["name"])
            if not x["passed"]:
                ever_failed.add(x["name"])
    inert = counted - ever_failed
    assert not inert, (
        f"{name} counts {sorted(inert)} as passed checks, but no point in "
        f"{len(rows)} fails them. A threshold nothing can fail is not a "
        f"protection - it must be marked disabled or given a real value.")
