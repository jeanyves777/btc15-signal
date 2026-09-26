"""Every instrument gates on its own measured numbers, in its own direction.

FIVE INSTRUMENTS NOW SHARE ONE RULE CLASS, and the thing that must not happen
is a threshold measured on one silently applying to another. That is not
hypothetical: gold's `brti_normalized_distance` reads 0.83 where BTC's reads
6.92 for risk-equivalent setups, so BTC's 10-15x floor rejects 99.4% of gold.

The subtler version is DIRECTION. BTC wants momentum and acceleration HIGH -
its edge is a move that already happened and left the strike behind. Silver
loses at |momentum| >= 10bp (-0.0745) and at accel >= +10 (-0.0657); gold's
-10..0 accel bucket (+0.1474) beats its 0..+10 (+0.0754). On those instruments
a hard-accelerating move is evidence AGAINST the trade. Copying BTC's floor
across would gate for the opposite of what the data supports, and it would look
entirely deliberate.

These tests pin that each config carries its own numbers, that ceilings only
exist where measured, and that BTC and ETH are bit-identical to before.

STATUS as at 2026-09-25, recorded so no test is read as an endorsement, and
kept current because a stale status line here is the very defect these tests
pin:

  GOLD    tier A on the merged corpus and it replicates on the near-disjoint
          sample; automation OFF (approval only).
  SILVER  clears every requirement on the fitted sample AND both halves, but
          does NOT replicate on the older sub-corpus, where it spans zero.
          Automation OFF. Provisional, not established.
  SOL     no measured edge at all - ungated residual +0.0013 over 1,844
          markets, and no gate set of 4,860 beat taking everything. It TRADES
          LIVE WITH AUTOMATION ON anyway, by the operator's decision of
          2026-09-25 against that measurement, logged with the evidence in
          FINDINGS 75c. The finding is not withdrawn by the decision.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.kalshi_brti import KalshiBRTIRule  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = {
    "GOLD": "strategy_kalshi_gold.json",
    "SILVER": "strategy_kalshi_silver.json",
    "SOL": "strategy_kalshi_sol.json",
}


def rule_for(name: str) -> KalshiBRTIRule:
    cfg = {k: v for k, v in
           json.loads((ROOT / CONFIGS[name]).read_text()).items()
           if not k.startswith("_")}
    return KalshiBRTIRule(**cfg)


# ------------------------------------------------- each has its own numbers

def test_every_instrument_declares_its_asset():
    """`surface.asset` drives the corpus guard as well as the label - an
    instrument it does not recognise has its guard silently disabled."""
    from btc15_signal import surface
    for name in CONFIGS:
        assert rule_for(name).asset == name
    assert surface.asset("KXSILVER15M") == "SILVER"
    assert surface.asset("KXSOL15M") == "SOL"
    assert surface.asset("KXGOLD15M") == "GOLD"


def test_no_instrument_inherits_btcs_price_band():
    """The exact bands are a measurement and move when re-measured, so pinning
    them only means editing this test to match whatever shipped. What must hold
    is that none of the three is BTC's band - that would mean an instrument
    being traded on another instrument's evidence, which is FINDINGS 43."""
    btc = KalshiBRTIRule()
    assert (btc.min_ask, btc.max_ask) == (0.70, 0.93), "BTC band moved"
    for name in CONFIGS:
        r = rule_for(name)
        assert (r.min_ask, r.max_ask) != (btc.min_ask, btc.max_ask), name
        assert 0.0 < r.min_ask < r.max_ask <= 1.0, name


def test_each_config_names_the_evidence_behind_it():
    """A number without its measurement is a number nobody can re-check."""
    for name, path in CONFIGS.items():
        raw = json.loads((ROOT / path).read_text())
        assert any(k.startswith("_") for k in raw), name
        blob = " ".join(str(v) for k, v in raw.items() if k.startswith("_"))
        assert "residual" in blob or "+0." in blob, name


def test_silver_and_sol_record_that_they_are_not_established():
    """Neither met the bar gold did. Silver reaches every requirement on the
    fitted sample but does not replicate on the near-disjoint one; SOL has no
    measurable edge at all - its ungated residual is +0.0013 over 1,844
    markets. A config that does not SAY so invites a later reader to treat it
    as established, which is how the previous numbers came to be quoted as
    findings. The caveat is matched by substance rather than by one phrase, so
    rewording it is allowed and dropping it is not."""
    caveats = ("provisional", "data capture", "no measured edge",
               "not replicated", "spans zero", "spanning zero", "decay")
    for name in ("SILVER", "SOL"):
        blob = (ROOT / CONFIGS[name]).read_text(encoding="utf-8").lower()
        hits = [c for c in caveats if c in blob]
        assert len(hits) >= 2, f"{name} records only {hits}"
    # SILVER still has automation off. SOL does NOT, since 2026-09-25: the
    # operator enabled live trading against the measurement. The caveat above
    # still has to be there - the finding is not withdrawn by the decision -
    # but the config must say what is DEPLOYED, so it may no longer claim
    # automation is off while real orders go out.
    silver = (ROOT / CONFIGS["SILVER"]).read_text(encoding="utf-8").lower()
    assert "automation off" in silver
    sol = (ROOT / CONFIGS["SOL"]).read_text(encoding="utf-8").lower()
    assert "automation on" in sol, "SOL trades live; the config must say so"
    assert "data capture only - automation off" not in sol


# -------------------------------------- direction is measured, not copied

def features(**over):
    class F:
        brti_normalized_distance = 0.5
        signed_distance_bps = 1.2
        brti_volatility_bps = 9.7
        brti_momentum_bps = 1.0
        brti_accel = 2.0
        brti_held_s = 80
        brti_rejections = 12
        brti_retrace = 0.1
        brti_choppiness = 0.5
        side = "UP"
        stale = False
        samples = 900
        value = 64.0
        target = 63.99
    for k, v in over.items():
        setattr(F, k, v)
    return F()


def fact(rule, feats, name, ask=0.55, remaining=480):
    return next(f for f in rule.check_facts(feats, ask, remaining)
                if f["name"] == name)


def test_silver_caps_momentum_where_btc_floors_it():
    """Silver's edge dies in fast moves; BTC's edge IS the fast move. So the
    cap has to exist and has to bind above itself, whatever value it holds."""
    rule = rule_for("SILVER")
    assert rule.max_brti_momentum_bps is not None
    fast = features(brti_momentum_bps=rule.max_brti_momentum_bps + 5.0)
    assert not fact(rule, fast, "BRTI momentum",
                    ask=(rule.min_ask + rule.max_ask) / 2)["passed"]
    assert fact(KalshiBRTIRule(), fast, "BRTI momentum", ask=0.80)["passed"]


def test_silver_caps_acceleration_where_btc_floors_it():
    """On silver a hard-accelerating move is evidence AGAINST the trade; on BTC
    it is the trade. Derived from the config so a re-measurement of the cap
    does not turn this into a test of a stale number."""
    rule = rule_for("SILVER")
    assert rule.max_brti_accel is not None
    hard = features(brti_accel=rule.max_brti_accel + 10.0)
    mid = (rule.min_ask + rule.max_ask) / 2
    assert not fact(rule, hard, "Move still working", ask=mid)["passed"]
    assert fact(KalshiBRTIRule(), hard, "Move still working",
                ask=0.80)["passed"]


def test_a_ceiling_breach_is_not_reported_as_decay():
    """The wording must name the end that failed. "+20 bps - the move is
    decaying" says the opposite of what happened."""
    rule = rule_for("SILVER")
    hard = features(brti_accel=(rule.max_brti_accel or 10.0) + 10.0)
    text = fact(rule, hard, "Move still working",
                ask=(rule.min_ask + rule.max_ask) / 2)["fail_text"]
    assert "accelerating too hard" in text
    assert "decaying" not in text


def test_the_distance_gradient_runs_opposite_to_btcs():
    """These instruments hold their level rather than run from it, so a LARGE
    distance means the move already happened - which is BTC's edge, not theirs.
    The ceiling therefore has to exist and bind, where BTC has none at all."""
    for name in CONFIGS:
        rule = rule_for(name)
        assert rule.max_abs_distance_bps is not None, name
        far = features(signed_distance_bps=rule.max_abs_distance_bps + 2.0)
        mid = (rule.min_ask + rule.max_ask) / 2
        assert not fact(rule, far, "BRTI distance", ask=mid)["passed"], name
    assert KalshiBRTIRule().max_abs_distance_bps is None


# ------------------------------------------------- BTC and ETH unchanged

def test_btc_and_eth_have_no_ceilings_at_all():
    """Every ceiling defaults to None, so adding them cannot have altered the
    two instruments that were already trading."""
    for rule in (KalshiBRTIRule(),
                 KalshiBRTIRule(**{k: v for k, v in json.loads(
                     (ROOT / "strategy_kalshi_eth.json").read_text()).items()
                     if not k.startswith("_")})):
        assert rule.max_brti_momentum_bps is None
        assert rule.max_brti_accel is None
        assert rule.max_abs_distance_bps is None
        assert rule.min_abs_distance_bps is None


def test_eth_config_did_not_acquire_any_new_key():
    cfg = json.loads((ROOT / "strategy_kalshi_eth.json").read_text())
    for forbidden in ("max_brti_momentum_bps", "max_brti_accel",
                      "min_abs_distance_bps", "max_abs_distance_bps"):
        assert forbidden not in cfg


# ----------------------------------------------------- instance isolation

def test_each_launcher_isolates_every_shared_path():
    for name, script in (("silver", "run_silver.ps1"), ("sol", "run_sol.ps1")):
        text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        for needed in (f'BTC15_INSTANCE       = "{name}"',
                       f"runtime-{name}/intelligence_policy.json",
                       f"runtime-{name}/settlement_reference.db",
                       # The corpus filename is NOT pinned exactly: it moved to
                       # the merged corpus on 2026-09-25, the only one carrying
                       # brti_retrace. What must hold is that it names THIS
                       # instrument, which `test_no_launcher_points_at_another_
                       # instruments_corpus` checks by pattern.
                       f"data/brti_history_{name}",
                       'TELEGRAM_COMMANDS_ENABLED = "false"'):
            assert needed in text, f"{script}: {needed}"


def test_no_launcher_points_at_another_instruments_corpus():
    """`_corpus_mismatch` catches this at fit time, but a launcher that names
    the wrong corpus is a defect whether or not something else stops it."""
    import re
    for name in ("gold", "silver", "sol", "eth"):
        script = ROOT / "scripts" / f"run_{name}.ps1"
        if not script.exists():
            continue
        text = script.read_text(encoding="utf-8")
        m = re.search(r'CORPUS_BRTI_PATH\s*=\s*"([^"]+)"', text)
        assert m, name
        assert name in m.group(1).lower(), f"{name} -> {m.group(1)}"


# ------------------------------------------- the message must not confuse

def test_the_open_position_figure_excludes_untraded_series():
    """A BTC alert reported "Open position: -$2.89" that was entirely three
    KXMVECROSSCATEGORY positions of the operator's - none of it the bot's.
    Third instance of the account-wide scope error, after the money footer
    and preflight."""
    from btc15_signal.main import _scoped_open
    mixed = {"KXMVECROSSCATEGORY-a": -0.95, "KXMVECROSSCATEGORY-b": -0.99,
             "KXBTC15M-26SEP251500-00": +0.12}
    assert _scoped_open(mixed) == (1, 0.12)
    assert _scoped_open({"KXMVECROSSCATEGORY-a": -0.95}) == (0, 0)
    assert _scoped_open(None) == (0, 0)


def test_the_decline_list_uses_the_same_names_as_the_ticks():
    """One message showed "❌ Price" and "❌ Distance" above, then "Auto
    declined: Decision ask · BRTI distance" below. Four names, two gates."""
    from btc15_signal import surface
    assert surface.display_name("Decision ask") == "Price"
    assert surface.display_name("BRTI distance") == "Distance"
    source = (ROOT / "src" / "btc15_signal" / "main.py").read_text(
        encoding="utf-8")
    assert "surface.display_name(str(fact[\"name\"]))" in source


def test_a_passing_negative_accel_does_not_read_as_a_contradiction():
    """"✅ Move still working: -4.7 bps accel" says the move is alive and
    shows a number saying it is decaying. Both are true - it is easing within
    tolerance - and the text has to say which."""
    rule = KalshiBRTIRule()
    easing = features(brti_accel=-4.7)
    fact = next(f for f in rule.check_facts(easing, 0.80, 600)
                if f["name"] == "Move still working")
    assert fact["passed"]
    assert "easing" in fact["pass_text"]
    assert "tolerance" in fact["pass_text"]
    building = features(brti_accel=+8.0)
    good = next(f for f in rule.check_facts(building, 0.80, 600)
                if f["name"] == "Move still working")
    assert "easing" not in good["pass_text"]


def test_each_instrument_reports_its_OWN_signal_record(tmp_path):
    """It was a rotating insight, so whether a reader could see the call record
    depended on which variant came up. With five instruments running it is the
    number that says whether a new one works at all.

    ON A TEMPORARY DATABASE, never the live one. This test used to do
    `Store(ROOT / "gold15.db")`, which opens the production database of a
    RUNNING service read-write and runs the store's schema DDL against it - so
    running the suite mutated a live trading database. It also asserted
    `settled > 0` on live rows, which made its verdict depend on how trading
    happened to be going, and made it silently pass by `return` whenever the
    file was missing. Two rows from two series test the same property and fail
    if the scoping is ever dropped."""
    from btc15_signal.store import Store

    class FS:
        kalshi_series = "KXGOLD15M"

    store = Store(str(tmp_path / "scoping.db"))
    store.db.executemany(
        "INSERT INTO predictions (window_open, created_at, target, entry_price, "
        "side, contract_ticker, won) VALUES (?,?,?,?,?,?,?)",
        [
            (1, 1, 64.0, 64.0, "UP", "KXGOLD15M-26SEP251500-00", 1),
            (2, 2, 64.0, 64.0, "UP", "KXGOLD15M-26SEP251515-15", 0),
            (3, 3, 64.0, 64.0, "UP", "KXBTC15M-26SEP251500-00", 1),
            (4, 4, 64.0, 64.0, "UP", "KXBTC15M-26SEP251515-15", 1),
            # ungraded: must be counted by neither
            (5, 5, 64.0, 64.0, "UP", "KXGOLD15M-26SEP251530-30", None),
        ],
    )
    store.db.commit()

    store.configure_instrument(FS())
    scoped = store.signal_record()
    store.instrument_series = None
    whole = store.signal_record()
    store.db.close()

    assert scoped == {"settled": 2, "wins": 1}, "gold's own calls only"
    assert whole == {"settled": 4, "wins": 3}, "every graded call"
    assert scoped["settled"] < whole["settled"], \
        "the scoped record must not exceed the unscoped one"


def test_the_signal_line_is_always_rendered_not_rotated():
    from btc15_signal import surface

    class Snap:
        realised = 0.0
        markets = 0
        winners = 0
        losers = 0
        lifetime = None
        open_position = None
        sessions = ()
        signal_record = {"settled": 63, "wins": 49}
    text = "\n".join(surface.money_footer(Snap()))
    assert "Signals" in text
    assert "49W" in text and "14L" in text


def test_no_signal_line_when_nothing_has_settled():
    """A percentage over zero settled is noise presented as a measurement."""
    from btc15_signal import surface

    class Snap:
        realised = 0.0
        markets = 0
        winners = 0
        losers = 0
        lifetime = None
        open_position = None
        sessions = ()
        signal_record = {"settled": 0, "wins": 0}
    assert "Signals" not in "\n".join(surface.money_footer(Snap()))

def test_no_config_advertises_a_window_the_service_cannot_act_on():
    """ENTRY IS GATED TWICE, INDEPENDENTLY, and only the intersection runs.
    `settings.entry_from_seconds`/`entry_to_seconds` (660/360 by default, and no
    launcher overrides them) is checked in main.py BEFORE the rule is consulted;
    the rule's own two keys are checked again in kalshi_signal and kalshi_brti.

    SOL's config said 800-250s while the service could only ever act between
    660s and 360s. That cost nothing at the time - the corpora hold decision
    points at 360..660 only, so the fitted windows selected exactly the minutes
    that ran - but it is a live trap: widening the Settings window later would
    have started SOL trading at 780, 720, 300 and 250 seconds, none of which any
    corpus point covers and none of which was ever measured.

    So a config may narrow the service window and must never claim to widen it.
    FINDINGS 77."""
    from btc15_signal.config import Settings
    s = Settings()
    for name in CONFIGS:
        rule = rule_for(name)
        assert rule.entry_from_seconds <= s.entry_from_seconds, (
            f"{name} claims to start at {rule.entry_from_seconds}s but the "
            f"service never evaluates before {s.entry_from_seconds}s")
        assert rule.entry_to_seconds >= s.entry_to_seconds, (
            f"{name} claims to run until {rule.entry_to_seconds}s but the "
            f"service stops evaluating at {s.entry_to_seconds}s")
        assert rule.entry_to_seconds < rule.entry_from_seconds, name
