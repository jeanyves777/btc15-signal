"""The confidence word is shown as arithmetic, and one trade gets one word.

THE REPORT. KXBTC15M-26SEP240915-15 alerted "Confidence MEDIUM · Entry checks
5/5" and the operator asked why choppiness was not among the checks. It is
not a check by their own instruction - it influences confidence only. But it
was also not the cause: the sum was

    checks 100 · shield -6 · clock -11 = 83,  HIGH starts at 85

and choppiness (-7) took it to 76 without changing the label. Three terms
move this word and none of them was visible, so the header could only be
checked by reading the source.

THE SECOND DEFECT, found while fixing the first. `fill_message` recomputed
the label with NO deltas while the signal passed both, so the same trade
could alert MEDIUM and fill HIGH - the thing `signal_message` states in its
own docstring cannot happen.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import btc15_signal.main as main  # noqa: E402
from btc15_signal import surface  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.regime import HIGH_AT  # noqa: E402

S = Settings()
OPENED = 1_790_254_800_000          # the window in the report
FIVE = [{"name": str(i), "passed": True} for i in range(5)]


# ------------------------------------------------------- the reported case

def test_the_reported_window_reproduces():
    chop = 0.4647
    b = main.confidence_breakdown(
        FIVE, OPENED, None, 0, chop,
        main.choppiness_points(chop, S.choppiness_penalty))
    assert b["checks"] == 100
    assert b["shield"] == -6
    assert b["clock"] == -11
    assert b["choppiness_points"] == -7
    assert b["points"] == 76
    assert b["label"] == "MEDIUM"


def test_choppiness_was_not_what_made_it_medium():
    """The operator's reading was the natural one and it was not the cause.
    Without choppiness at all the score is 83, still under the 85 for HIGH."""
    b = main.confidence_breakdown(FIVE, OPENED, None, 0, None, 0)
    assert b["points"] == 83
    assert b["label"] == "MEDIUM"
    assert b["points"] < HIGH_AT


def test_the_breakdown_always_equals_the_label():
    """The note and the word are two views of one number, or the note is
    worse than nothing."""
    for chop in (None, 0.0, 0.2, 0.4647, 0.9, 1.0):
        pts = main.choppiness_points(chop, S.choppiness_penalty)
        for intel in (-10, 0, 7):
            b = main.confidence_breakdown(FIVE, OPENED, None, intel, chop, pts)
            assert b["label"] == main.confidence_label(
                FIVE, OPENED, None, intel, pts)


def test_the_terms_sum_to_the_score():
    b = main.confidence_breakdown(FIVE, OPENED, None, 5, 0.5, -8)
    total = (b["checks"] + b["clock"] + b["shield"] + b["intelligence"]
             + b["choppiness_points"])
    assert b["points"] == max(0, min(100, total))


# ------------------------------------------------------------- the render

def test_the_note_names_every_term():
    b = main.confidence_breakdown(FIVE, OPENED, None, 0, 0.4647, -7)
    note = surface.confidence_note(**{k: v for k, v in b.items()
                                      if k != "label"})
    for token in ("76/100", "HIGH at 85", "checks +100", "shield -6",
                  "clock -11", "chop -7", "46%"):
        assert token in note, (token, note)


def test_an_unmeasured_window_says_so_rather_than_zero():
    b = main.confidence_breakdown(FIVE, OPENED, None, 0, None, 0)
    note = surface.confidence_note(**{k: v for k, v in b.items()
                                      if k != "label"})
    assert "chop n/a" in note
    assert "0%" not in note


def test_the_learned_term_appears_only_when_it_did_something():
    quiet = surface.confidence_note(
        points=83, high_at=85, checks=100, clock=-11, shield=-6,
        intelligence=0, choppiness=0.1, choppiness_points=-2)
    loud = surface.confidence_note(
        points=90, high_at=85, checks=100, clock=-11, shield=-6,
        intelligence=7, choppiness=0.1, choppiness_points=-2)
    assert "learned" not in quiet
    assert "learned +7" in loud


# ------------------------------- one trade, one word: signal and fill agree

def test_the_fill_uses_the_same_computation_as_the_signal():
    import inspect

    source = inspect.getsource(main.primary_signal)
    assert "confidence=fill_confidence[\"label\"]" in source
    # and the old deltas-free call is gone
    assert "confidence_label(\n                                        fill_facts" not in source
    at = source.index("fill_confidence = confidence_breakdown(")
    window = source[at:at + 420]
    assert "intel_verdict.confidence_delta" in window
    assert "choppiness_points(" in window


def test_both_messages_accept_the_note():
    import inspect

    from btc15_signal import messages

    for fn in (messages.signal_message, messages.fill_message):
        assert "confidence_note" in inspect.signature(fn).parameters


# ------------------------------------ it is still not a gate, and cannot be

def test_the_note_is_not_a_check():
    """It renders in `essentials`, never in the checks block, so nothing here
    can be read as a gate that passed or failed."""
    import inspect

    from btc15_signal import messages

    for fn in (messages.signal_message, messages.fill_message):
        source = inspect.getsource(fn)
        # The USE, not the signature - rindex, because the parameter name is
        # the first occurrence in every one of these functions.
        at = source.rindex("if confidence_note:")
        after = source[at:at + 120]
        assert "essentials.append" in after
        assert "checks=" not in after


def test_choppiness_still_appears_in_no_gate():
    from btc15_signal.kalshi_brti import KalshiBRTIRule

    import dataclasses

    fields = {f.name for f in dataclasses.fields(KalshiBRTIRule)}
    assert not any("chop" in n for n in fields), fields


# ----------------------------- a refused setup is not a MEDIUM one

def test_a_setup_that_failed_a_gate_is_capped_low():
    """Operator, 2026-09-24, KXBTC15M-26SEP241015-15: 65c against a 70-93c
    band and 3.5x against a 10x floor - two gates failed - and the header
    still read MEDIUM, because 3 of 5 agreeing scores 70 and the clock added
    21 on top."""
    facts = [{"name": str(i), "passed": i < 3} for i in range(5)]
    b = main.confidence_breakdown(facts, OPENED, None, 0, 0.63, -10)
    assert b["failed"] == 2
    assert b["label"] == "LOW"


def test_one_failed_gate_is_enough():
    facts = [{"name": str(i), "passed": i < 4} for i in range(5)]
    assert main.confidence_breakdown(facts, OPENED, None)["label"] == "LOW"


def test_a_clean_setup_is_not_capped():
    b = main.confidence_breakdown(FIVE, OPENED, None, 0, 0.30, -5)
    assert b["failed"] == 0
    assert b["label"] in ("MEDIUM", "HIGH")


def test_the_points_are_NOT_changed_by_the_cap():
    """`model_confidence_points` feeds the training corpus through the same
    `regime_model_points`; moving it would silently re-score every row the
    calibration compares against. The cap is on the WORD only."""
    facts = [{"name": str(i), "passed": i < 3} for i in range(5)]
    b = main.confidence_breakdown(facts, OPENED, None)
    assert b["points"] == main.model_confidence_points(facts, OPENED, None)


def test_the_cap_is_explained_on_the_score_line():
    """A score of 75 printed beside the word LOW is a contradiction unless
    the reason is on the same line."""
    facts = [{"name": str(i), "passed": i < 3} for i in range(5)]
    b = main.confidence_breakdown(facts, OPENED, None, 0, 0.63, -10)
    note = surface.confidence_note(**{k: v for k, v in b.items()
                                      if k != "label"})
    assert "capped LOW" in note
    assert "2 gates failed" in note
    assert "checks +70" in note          # the arithmetic is still shown


def test_the_singular_reads_correctly():
    facts = [{"name": str(i), "passed": i < 4} for i in range(5)]
    b = main.confidence_breakdown(facts, OPENED, None)
    note = surface.confidence_note(**{k: v for k, v in b.items()
                                      if k != "label"})
    assert "1 gate failed" in note


# --------------- the status line names EVERY condition, not just the gates

def test_the_declined_status_reports_more_than_the_checks():
    """Operator, 2026-09-24, KXBTC15M-26SEP241030-30. The alert showed
    "Band held: 0s of 60s" and, directly beneath it, "Auto declined: 2 checks
    failed" - a line that did not mention the band-hold timer at all. Read
    together they say the five checks are the whole test, and they are not:
    the timer, the account limits, the one-position guard and an
    intelligence veto all refuse a setup the gates accepted."""
    import inspect

    source = inspect.getsource(main.primary_signal)
    at = source.index("Auto declined:")
    # walk back to the branch that builds it
    branch = source[max(0, at - 1800):at + 400]
    assert "entry_band_settle_s" in branch, "band-hold timer not reported"
    assert "auto_block_reason" in branch, "account limits not reported"
    assert "intelligence veto" in branch, "veto not reported"


def test_the_declined_status_counts_conditions_not_checks():
    import inspect

    source = inspect.getsource(main.primary_signal)
    at = source.index("Auto declined:")
    assert "condition" in source[at:at + 200]


def test_the_extra_reads_are_on_the_alerting_path_only():
    """They cost a query. That is affordable where a message is composed and
    not where an order goes out - the gap between decision and submit is the
    largest known cause of missed fills (FINDINGS 22)."""
    import inspect

    source = inspect.getsource(main.primary_signal)
    declined = source.index("Auto declined:")
    submit = source.index("execute_with_take_profit")
    assert submit < declined, "the status block must sit after the order path"
