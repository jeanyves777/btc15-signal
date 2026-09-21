"""Confidence must count INDEPENDENT signals, and a thin gap is not agreement.

KXBTC15M-26SEP211445-45 on 2026-09-21 was announced "BUY - confidence high (5/5
signals agree)" on a distance of 1.9x the recent 5-minute move, against a rule
floor of 1.5x. The evidence line in the same message called that gap
"moderate". One minute later BTC was 83 cents from the target and the market
had flipped to 68% the other way.

Two of those five could not disagree:
  * `signed > 0` is true by construction - the side is CHOSEN from the sign of
    the distance - so it scored on every entry ever alerted.
  * `vol_units >= 1.5` restated the rule's own distance gate, already counted
    by `rule_match`, and ticked the same at 1.9x as at 6x.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.decision import decision_facts  # noqa: E402


def facts(*, btc, target, volatility_5m_bps, momentum_5m_bps=-12.3):
    """The live 14:45 setup, with only the gap varied.

    Momentum is negative because the side is DOWN: `decision_facts` signs it
    for our side, so -12.3 is momentum pushing our way.
    """
    return decision_facts(
        ticker="KXBTC15M-26SEP211445-45", remaining_s=422, side="DOWN",
        btc=btc, target=target,
        our_ask=0.79, exit_bid=0.78, yes_bid=0.20, yes_ask=0.21, no_ask=0.79,
        yes_levels=[(0.20, 1000.0)], no_levels=[(0.78, 9000.0)],
        momentum_5m_bps=momentum_5m_bps, volatility_5m_bps=volatility_5m_bps,
        futures_basis_bps=1.0, taker_imbalance=0.2, spread_bps=1.0,
        session="us", vol_regime="mid", book_age_s=8.0,
        rule_match=True, failed_gates="",
        holding=False, entry_paid=None, unrealised=None,
        model_probability=0.94, measured_edge=0.0166, slippage=0.01,
    )


# 1.91x - under the band, and the live setup that was called 5/5 HIGH.
THIN = dict(btc=85_831.73, target=85_917.19, volatility_5m_bps=5.2)
# ~3.0x - inside the measured 2-4x edge band.
MID = dict(btc=85_783.00, target=85_917.19, volatility_5m_bps=5.2)
# ~13.8x - far ABOVE the band. Not "extra safe": measured at +0.0064/contract
# against +0.0359 inside the band, because a strike that far away is already
# priced for the safety it offers.
WIDE = dict(btc=85_300.00, target=85_917.19, volatility_5m_bps=5.2)


def test_the_live_setup_is_no_longer_called_high_confidence():
    """The exact numbers: 85 dollars from target, 1.9x the 5-minute move."""
    f = facts(**THIN)
    assert 1.5 <= f["price"]["distance_vol_units"] < 3.0
    assert f["verdict"]["confidence"] != "HIGH", (
        "a gap the prose calls moderate must not be HIGH"
    )
    assert f["verdict"]["signals_total"] == 4


def test_a_gap_inside_the_measured_band_reads_high():
    """The fix must tighten the count, not disable it."""
    f = facts(**MID)
    assert 2.0 <= f["price"]["distance_vol_units"] < 4.0
    assert f["verdict"]["confidence"] == "HIGH"


def test_a_gap_far_beyond_the_band_does_not_score_the_distance_point():
    """"More distance is better" was wrong. Over 3,841 deployed entries the
    edge peaks at 2-4x (+0.0359/contract) and decays above it (5x+ measures
    +0.0064) - a strike far enough away to be safe is already priced for it.
    The old >=3x rule scored a 13x setup as confidently as a 3x one."""
    f = facts(**WIDE)
    assert f["price"]["distance_vol_units"] >= 4.0
    wide = f["verdict"]["signals_agreeing"]
    mid = facts(**MID)["verdict"]["signals_agreeing"]
    assert wide < mid, "a gap outside the band must score below one inside it"


def test_no_signal_in_the_count_is_true_by_construction():
    """`signed > 0` held on every entry the bot ever alerted, so a setup that
    is wrong on everything except the rule must not score more than one."""
    f = decision_facts(
        ticker="T", remaining_s=422, side="DOWN",
        btc=85_831.73, target=85_917.19,
        our_ask=0.79, exit_bid=0.78, yes_bid=0.20, yes_ask=0.21, no_ask=0.79,
        yes_levels=[(0.20, 9000.0)], no_levels=[(0.78, 1000.0)],  # book against
        momentum_5m_bps=+12.3,  # momentum against a DOWN bet
        volatility_5m_bps=5.2, futures_basis_bps=1.0, taker_imbalance=-0.2,
        spread_bps=1.0, session="us", vol_regime="mid", book_age_s=8.0,
        rule_match=True, failed_gates="", holding=False, entry_paid=None,
        unrealised=None, model_probability=0.94, measured_edge=0.0166,
        slippage=0.01,
    )
    assert f["verdict"]["signals_agreeing"] == 1
    assert f["verdict"]["confidence"] == "LOW"


def test_the_number_and_the_prose_agree_about_the_gap():
    """The old thresholds let "5/5 signals agree" sit directly above a line
    calling the same gap moderate. They now share the 3x boundary."""
    thin, mid = facts(**THIN), facts(**MID)
    assert any("moderate" in e for e in thin["summary"]["evidence"])
    assert any("comfortable" in e for e in mid["summary"]["evidence"])
    assert thin["verdict"]["confidence"] != "HIGH"
    assert mid["verdict"]["confidence"] == "HIGH"
